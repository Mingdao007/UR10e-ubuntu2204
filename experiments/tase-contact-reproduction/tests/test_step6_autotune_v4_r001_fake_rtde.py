"""Behavior tests for the offline-only Step6 r001 FakeRTDE seam."""

from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal

import pytest

from step6_autotune_v4_r001 import (
    AnchorAuditReceipt,
    AnchorAuditStatus,
    AttemptDisposition,
    CampaignContractError,
    CampaignGenesis,
    CampaignPhase,
    CampaignState,
    CampaignStateError,
    FakeRTDEError,
    FakeRTDEEvidenceError,
    FakeRTDEFreshnessError,
    FakeRTDEIdentityError,
    FakeRTDETransport,
    FORMAL_BIN_COUNT,
    ForceSample,
    ForceSampleIdentity,
    ForceObjectiveReceipt,
    IncumbentSeedReceipt,
    Named7dCandidate,
    Named7dDomain,
    MIN_OBSERVATION_DURATION_S,
    OfflineRtdeCommand,
    OfflineRtdeCommandPayload,
    OfflineRtdeEvidenceReceipt,
    OfflineRtdeFreshnessPolicy,
    OfflineRtdeRouteIdentity,
    OfflineRtdeTransportPacket,
    STEP6_R001_PROFILE,
    build_force_objective_receipt,
    build_offline_rtde_evidence_receipt,
    assess_attempt,
    plan_bootstrap_pd,
)


_CAMPAIGN = "a" * 64
_SOURCE_CLOSURE = "b" * 64
_PACKAGE_ROOT = "c" * 64
_SEED_CANDIDATE = "d" * 64
_HANDOFF = "e" * 64


def _candidate(
    *,
    P: float = 0.0,
    D: float = 0.0,
    tau: float = 0.5,
    I_on_log2: float = 1.0,
    I_off: int = 0,
    Ko: float = 0.25,
    Kp: float = 2.0,
) -> Named7dCandidate:
    return Named7dCandidate(
        P=P,
        D=D,
        tau=tau,
        I_on_log2=I_on_log2,
        I_off=I_off,
        Ko=Ko,
        Kp=Kp,
    )


def _route(
    *,
    campaign_fingerprint: str = _CAMPAIGN,
    epoch: int = 1,
    writer_id: str = "step6-writer-b",
    route_id: str = "offline-step6-route",
    candidate: Named7dCandidate | None = None,
    attempt_identity: str = "1" * 64,
) -> OfflineRtdeRouteIdentity:
    return OfflineRtdeRouteIdentity(
        campaign_fingerprint=campaign_fingerprint,
        epoch=epoch,
        writer_id=writer_id,
        route_id=route_id,
        candidate=_candidate() if candidate is None else candidate,
        attempt_identity=attempt_identity,
    )


def _policy(*, timeout: float = 0.03, start: float = 0.0, end: float = 0.1) -> OfflineRtdeFreshnessPolicy:
    return OfflineRtdeFreshnessPolicy(
        command_age_timeout_s=timeout,
        observation_start_s=start,
        observation_end_s=end,
    )


def _payload(
    *,
    path_time_s: float = 0.0,
    continuous_phase_rad: float | None = None,
    desired_base_x_m: float = 0.0,
    desired_base_y_m: float = 0.0,
    desired_base_vx_m_s: float = 0.0,
    desired_base_vy_m_s: float = 0.0,
    program_z_delta_m: float = 0.0,
    force_target_n: float = 5.0,
) -> OfflineRtdeCommandPayload:
    return OfflineRtdeCommandPayload(
        path_time_s=path_time_s,
        continuous_phase_rad=(
            STEP6_R001_PROFILE.along_frequency_rad_s * path_time_s
            if continuous_phase_rad is None
            else continuous_phase_rad
        ),
        desired_base_x_m=desired_base_x_m,
        desired_base_y_m=desired_base_y_m,
        desired_base_vx_m_s=desired_base_vx_m_s,
        desired_base_vy_m_s=desired_base_vy_m_s,
        program_z_delta_m=program_z_delta_m,
        force_target_n=force_target_n,
    )


def _command(
    route: OfflineRtdeRouteIdentity,
    *,
    sequence: int = 1,
    published_time_s: float = 0.0,
    payload: OfflineRtdeCommandPayload | None = None,
) -> OfflineRtdeCommand:
    return OfflineRtdeCommand(
        route=route,
        command_sequence=sequence,
        published_time_s=published_time_s,
        payload=_payload() if payload is None else payload,
    )


def _packet(
    route: OfflineRtdeRouteIdentity,
    *,
    sequence: int,
    time_s: float,
    command: OfflineRtdeCommand | None = None,
) -> OfflineRtdeTransportPacket:
    return OfflineRtdeTransportPacket(
        route=route,
        transport_sequence=sequence,
        transport_time_s=time_s,
        command=_command(route) if command is None else command,
    )


def _complete_packets(
    route: OfflineRtdeRouteIdentity,
    policy: OfflineRtdeFreshnessPolicy,
    *,
    host_commands: bool = True,
    packet_count: int = 51,
    base_x: float = 0.0,
) -> tuple[OfflineRtdeTransportPacket, ...]:
    """Create a deterministic 500 Hz / 50 Hz-host or sparse-host interval."""

    duration = policy.observation_duration_s
    if packet_count < 2:
        raise ValueError("test packet population needs at least two points")
    command_cache: dict[int, OfflineRtdeCommand] = {}
    packets: list[OfflineRtdeTransportPacket] = []
    for index in range(packet_count):
        time_s = policy.observation_start_s + duration * index / (packet_count - 1)
        if host_commands:
            host_index = index // 10
            command_sequence = host_index + 1
            published_time_s = policy.observation_start_s + host_index / 50.0
        else:
            command_sequence = 1
            published_time_s = policy.observation_start_s
        command = command_cache.get(command_sequence)
        if command is None:
            command = _command(
                route,
                sequence=command_sequence,
                published_time_s=published_time_s,
                payload=_payload(
                    path_time_s=published_time_s,
                    desired_base_x_m=base_x,
                ),
            )
            command_cache[command_sequence] = command
        packets.append(
            _packet(
                route,
                sequence=index + 1,
                time_s=time_s,
                command=command,
            )
        )
    return tuple(packets)


def _collect(
    route: OfflineRtdeRouteIdentity,
    policy: OfflineRtdeFreshnessPolicy,
    packets: tuple[OfflineRtdeTransportPacket, ...] | None = None,
    *,
    base_x: float = 0.0,
) -> OfflineRtdeEvidenceReceipt:
    transport = FakeRTDETransport(route, policy)
    population = _complete_packets(route, policy, base_x=base_x) if packets is None else packets
    for packet in population:
        transport.observe(packet)
    return transport.finalize()


def test_500_hz_transport_allows_50_hz_host_reuse_and_binds_exact_versions() -> None:
    route = _route()
    policy = _policy()
    receipt = _collect(route, policy)

    assert receipt.packet_count == 51
    assert receipt.measured_transport_rate_hz == pytest.approx(500.0)
    assert len(receipt.retained_commands) == 6
    assert receipt.first_transport_time_s == 0.0
    assert receipt.last_transport_time_s == 0.1
    assert receipt.policy.policy_fingerprint == policy.policy_fingerprint
    assert receipt.transport_packet_sha256.islower()
    assert receipt.command_versions_sha256.islower()
    assert receipt.evidence_role == "diagnostic_only"


def test_full_60_second_500_hz_population_has_3001_host_versions() -> None:
    route = _route(route_id="full-60-second-route")
    policy = _policy(timeout=0.03, end=60.0)
    transport = FakeRTDETransport(route, policy)
    for packet in _complete_packets(route, policy, packet_count=30_001):
        transport.observe(packet)

    receipt = transport.finalize()
    assert receipt.packet_count == 30_001
    assert receipt.measured_transport_rate_hz == pytest.approx(500.0)
    assert len(receipt.retained_commands) == 3_001
    assert receipt.first_transport_time_s == 0.0
    assert receipt.last_transport_time_s == 60.0


def test_equal_command_sequence_payload_drift_and_command_rollback_fail_closed() -> None:
    route = _route()
    policy = _policy(timeout=0.2)
    first = _packet(route, sequence=1, time_s=0.0, command=_command(route))
    drifted = _packet(
        route,
        sequence=2,
        time_s=0.002,
        command=_command(
            route,
            payload=_payload(desired_base_x_m=0.001),
        ),
    )
    transport = FakeRTDETransport(route, policy)
    transport.observe(first)
    with pytest.raises(FakeRTDEFreshnessError, match="reason_43_equal_command_sequence_payload_drift"):
        transport.observe(drifted)

    rollback = FakeRTDETransport(route, policy)
    rollback.observe(_packet(route, sequence=1, time_s=0.0, command=_command(route, sequence=2)))
    with pytest.raises(FakeRTDEFreshnessError, match="reason_43_command_sequence_rollback"):
        rollback.observe(_packet(route, sequence=2, time_s=0.002, command=_command(route, sequence=1)))


def test_foreign_campaign_epoch_writer_or_route_is_rejected() -> None:
    route = _route()
    policy = _policy(timeout=0.2)
    changes = (
        {"campaign_fingerprint": "f" * 64},
        {"epoch": 2},
        {"writer_id": "foreign-writer"},
        {"route_id": "foreign-route"},
        {"candidate": _candidate(D=0.25)},
        {"attempt_identity": "2" * 64},
    )
    for change in changes:
        foreign = replace(route, **change)
        packet = _packet(foreign, sequence=1, time_s=0.0)
        transport = FakeRTDETransport(route, policy)
        with pytest.raises(FakeRTDEIdentityError):
            transport.observe(packet)


def test_route_fingerprint_binds_typed_candidate_and_attempt_identity() -> None:
    route = _route(candidate=_candidate(D=-0.25), attempt_identity="3" * 64)
    restored = OfflineRtdeRouteIdentity.from_mapping(route.to_mapping())
    assert restored == route
    assert restored.candidate.to_mapping() == route.candidate.to_mapping()
    assert restored.candidate_uid == route.candidate.candidate_uid
    assert restored.attempt_identity == "3" * 64

    with pytest.raises(FakeRTDEIdentityError):
        OfflineRtdeRouteIdentity(
            campaign_fingerprint=_CAMPAIGN,
            epoch=1,
            writer_id="writer",
            route_id="route",
            candidate=route.candidate.to_mapping(),  # type: ignore[arg-type]
            attempt_identity="4" * 64,
        )

    mapping = route.to_mapping()
    mapping["candidate_uid"] = "0" * 64
    with pytest.raises(FakeRTDEIdentityError):
        OfflineRtdeRouteIdentity.from_mapping(mapping)


def test_transport_sequence_and_time_duplicate_or_rollback_are_rejected() -> None:
    route = _route()
    policy = _policy(timeout=0.2)
    command = _command(route)

    duplicate = FakeRTDETransport(route, policy)
    duplicate.observe(_packet(route, sequence=1, time_s=0.0, command=command))
    with pytest.raises(FakeRTDEFreshnessError):
        duplicate.observe(_packet(route, sequence=1, time_s=0.002, command=command))

    rollback = FakeRTDETransport(route, policy)
    rollback.observe(_packet(route, sequence=2, time_s=0.002, command=command))
    with pytest.raises(FakeRTDEFreshnessError):
        rollback.observe(_packet(route, sequence=1, time_s=0.001, command=command))

    backwards_time = FakeRTDETransport(route, policy)
    backwards_time.observe(_packet(route, sequence=1, time_s=0.002, command=command))
    with pytest.raises(FakeRTDEFreshnessError):
        backwards_time.observe(_packet(route, sequence=2, time_s=0.001, command=command))


def test_rate_floor_passes_at_500_and_rejects_450_hz_evidence() -> None:
    route = _route()
    passing = _collect(route, _policy(timeout=0.03))
    assert passing.measured_transport_rate_hz >= 460.0

    slow_route = _route(route_id="slow-route")
    slow_policy = _policy(timeout=0.2)
    slow_transport = FakeRTDETransport(slow_route, slow_policy)
    for packet in _complete_packets(
        slow_route,
        slow_policy,
        host_commands=False,
        packet_count=46,
    ):
        slow_transport.observe(packet)
    with pytest.raises(FakeRTDEEvidenceError, match="below 460"):
        slow_transport.finalize()


def test_timeout_and_empty_or_short_window_produce_no_evidence() -> None:
    route = _route()
    policy = _policy(timeout=0.01)
    transport = FakeRTDETransport(route, policy)
    command = _command(route)
    transport.observe(_packet(route, sequence=1, time_s=0.0, command=command))
    with pytest.raises(FakeRTDEFreshnessError, match="timeout"):
        transport.observe(_packet(route, sequence=2, time_s=0.02, command=command))
    with pytest.raises(FakeRTDEFreshnessError):
        transport.finalize()

    empty = FakeRTDETransport(route, _policy(timeout=0.2))
    with pytest.raises(FakeRTDEEvidenceError):
        empty.finalize()

    short = FakeRTDETransport(route, _policy(timeout=0.2))
    short.observe(_packet(route, sequence=1, time_s=0.0))
    with pytest.raises(FakeRTDEEvidenceError):
        short.finalize()


@pytest.mark.parametrize(
    "start,end",
    [(0.0, 0.1), (5.0, 5.1), (59.9, 60.0)],
)
def test_exact_decimal_observation_widths_construct(start: float, end: float) -> None:
    accepted = _policy(start=start, end=end)
    assert accepted.observation_start_s == start
    assert accepted.observation_end_s == end


def test_observation_duration_just_under_one_force_mae_bin_rejects() -> None:
    with pytest.raises(FakeRTDEEvidenceError):
        _policy(start=5.0, end=5.099999)


def test_invalid_timestamps_and_locked_policy_geometry_fail_closed() -> None:
    route = _route()
    with pytest.raises(FakeRTDEFreshnessError):
        _command(route, published_time_s=-0.001)
    with pytest.raises(FakeRTDEFreshnessError):
        _command(route, published_time_s=math.nan)
    with pytest.raises(FakeRTDEFreshnessError):
        _packet(route, sequence=1, time_s=math.inf)
    with pytest.raises(FakeRTDEEvidenceError):
        OfflineRtdeFreshnessPolicy(command_age_timeout_s=0.1, observation_start_s=0.1, observation_end_s=0.1)
    with pytest.raises(FakeRTDEEvidenceError):
        OfflineRtdeFreshnessPolicy(
            command_age_timeout_s=0.1,
            observation_start_s=0.0,
            observation_end_s=0.1,
            min_transport_rate_hz=459.0,
        )
    with pytest.raises(FakeRTDEIdentityError):
        OfflineRtdeRouteIdentity(
            campaign_fingerprint=_CAMPAIGN,
            epoch=1,
            writer_id="writer",
            route_id="route",
            candidate=_candidate(),
            attempt_identity="5" * 64,
            schema_version=True,  # type: ignore[arg-type]
        )


def test_payload_canonicalization_is_order_stable_and_type_or_field_drift_rejects() -> None:
    payload = _payload(desired_base_x_m=0.25)
    mapping = payload.to_mapping()
    reordered = {key: mapping[key] for key in reversed(tuple(mapping))}
    restored = OfflineRtdeCommandPayload.from_mapping(reordered)
    assert restored.payload_sha256 == payload.payload_sha256
    assert restored.canonical_bytes() == payload.canonical_bytes()

    value_drift = dict(mapping)
    value_drift["desired_base_x_m"] = 0.5
    with pytest.raises(FakeRTDEError):
        OfflineRtdeCommandPayload.from_mapping(value_drift)

    for invalid in (True, 0, math.nan, math.inf):
        invalid_mapping = dict(mapping)
        invalid_mapping["desired_base_x_m"] = invalid
        with pytest.raises(FakeRTDEError):
            OfflineRtdeCommandPayload.from_mapping(invalid_mapping)

    missing = dict(mapping)
    del missing["force_target_n"]
    with pytest.raises(FakeRTDEError):
        OfflineRtdeCommandPayload.from_mapping(missing)
    unknown = dict(mapping)
    unknown["unexpected_field"] = 0.0
    with pytest.raises(FakeRTDEError):
        OfflineRtdeCommandPayload.from_mapping(unknown)
    with pytest.raises(FakeRTDEError):
        OfflineRtdeCommandPayload.from_mapping(payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path_time_s": -1e-9},
        {"path_time_s": 60.000001},
        {"continuous_phase_rad": 0.001},
        {"program_z_delta_m": 0.0347},
        {"program_z_delta_m": -0.0},
        {"force_target_n": 6.0},
        {"desired_base_vx_m_s": 0.0046},
    ],
)
def test_payload_binds_phase_target_z_time_and_reference_speed_invariants(kwargs: dict[str, object]) -> None:
    with pytest.raises(FakeRTDEError):
        _payload(**kwargs)  # type: ignore[arg-type]

    valid = _payload(path_time_s=12.5)
    with pytest.raises(FakeRTDEError):
        replace(valid, continuous_phase_rad=1.0)
    with pytest.raises(FakeRTDEError):
        replace(valid, force_target_n=6.0)
    with pytest.raises(FakeRTDEError):
        replace(valid, program_z_delta_m=0.0347)


@pytest.mark.parametrize("field_names", [None, 7, "not-a-field-list", {"path_time_s": 0.0}])
def test_payload_field_names_wrong_type_fails_with_typed_error(field_names: object) -> None:
    mapping = _payload().to_mapping()
    mapping["field_names"] = field_names
    with pytest.raises(FakeRTDEError):
        OfflineRtdeCommandPayload.from_mapping(mapping)


def test_evidence_receipt_is_builder_only_content_bound_and_diagnostic_only() -> None:
    route = _route()
    policy = _policy()
    packets = _complete_packets(route, policy)
    with pytest.raises(FakeRTDEEvidenceError):
        OfflineRtdeEvidenceReceipt(route=route, policy=policy, packets=packets)

    receipt = build_offline_rtde_evidence_receipt(route, policy, packets)
    assert receipt == build_offline_rtde_evidence_receipt(route, policy, list(packets))
    assert receipt.evidence_digest.islower()
    assert receipt.evidence_role == "diagnostic_only"
    for forbidden in (
        "objective_value_n",
        "optimizer_eligible",
        "return_receipt_sha256",
        "live_authority",
        "complete",
    ):
        assert not hasattr(receipt, forbidden)

    with pytest.raises(FakeRTDEEvidenceError):
        replace(receipt, policy=_policy(timeout=0.04))
    with pytest.raises(FakeRTDEEvidenceError):
        replace(receipt, packets=packets[:-1])
    with pytest.raises(FakeRTDEEvidenceError):
        replace(
            receipt,
            route=replace(
                route,
                candidate=_candidate(D=0.25),
                attempt_identity="6" * 64,
            ),
        )


def _campaign_state() -> CampaignState:
    candidate = Named7dCandidate(
        P=0.0,
        D=0.0,
        tau=0.5,
        I_on_log2=1.0,
        I_off=0,
        Ko=0.25,
        Kp=2.0,
    )
    seed = IncumbentSeedReceipt(
        candidate=candidate,
        source_candidate_uid=_SEED_CANDIDATE,
        source_identity="step5d.final.named_7d.incumbent",
        source_closure_sha256=_SOURCE_CLOSURE,
        handoff_sha256=_HANDOFF,
    )
    domain = Named7dDomain(
        P=(-0.25, 0.0, 0.25),
        D=(-0.25, 0.0, 0.25),
        tau=(0.5,),
        I_on_log2=(1.0,),
        I_off=(0,),
        Ko=(0.25,),
        Kp=(2.0,),
        source_identity="frozen.parent.domain.handoff",
        source_domain_sha256="f" * 64,
    )
    genesis = CampaignGenesis.create(
        campaign_id="fake-rtde-composition-campaign",
        epoch=1,
        source_closure_sha256=_SOURCE_CLOSURE,
        seed_receipt=seed,
        domain=domain,
    )
    plan = plan_bootstrap_pd(seed, domain)
    return CampaignState.create(genesis, plan, package_root_sha256=_PACKAGE_ROOT)


def _objective_receipt() -> ForceObjectiveReceipt:
    samples = [
        ForceSample(
            path_time_s=float(Decimal("5.05") + Decimal("0.1") * index),
            filtered_normal_n=5.0,
            sample_identity=ForceSampleIdentity(source_id="fake-rtde-objective", sequence=index),
        )
        for index in range(FORMAL_BIN_COUNT)
    ]
    return build_force_objective_receipt(samples)


def test_fake_rtde_is_diagnostic_only_in_full_campaign_composition() -> None:
    state = _campaign_state()
    objective_receipt = _objective_receipt()
    ticket = state.dispatch_next()
    assert ticket is not None
    route = _route(
        campaign_fingerprint=state.genesis.campaign_fingerprint,
        epoch=state.genesis.epoch,
        route_id=f"bootstrap-{ticket.bootstrap_index}",
        candidate=ticket.candidate,
        attempt_identity=ticket.attempt_identity,
    )
    transport_evidence = _collect(route, _policy(), base_x=ticket.candidate.P)
    assert transport_evidence.evidence_role == "diagnostic_only"
    with pytest.raises(CampaignContractError):
        assess_attempt(
            ticket.candidate,
            AttemptDisposition.OBJECTIVE,
            qualification_passed=True,
            objective_receipt=transport_evidence,  # type: ignore[arg-type]
        )

    assessment = assess_attempt(
        ticket.candidate,
        AttemptDisposition.OBJECTIVE,
        qualification_passed=True,
        objective_receipt=objective_receipt,
    )
    assert assessment.objective_receipt is objective_receipt
    with pytest.raises(CampaignStateError):
        state.record_attempt(
            ticket,
            assessment,
            return_receipt_sha256=transport_evidence,  # type: ignore[arg-type]
        )
    assert state.attempts == ()
    paused = state.record_attempt(
        ticket,
        assessment,
        return_receipt_sha256="1" * 64,
    )
    assert paused.phase is CampaignPhase.PAUSED_FOR_ANCHOR_AUDIT
    assert paused.pause_context is not None
    assert paused.pause_context.return_receipt_sha256 == "1" * 64

    resumed = paused.resume_from_anchor_audit(
        AnchorAuditReceipt.from_context(paused.pause_context, AnchorAuditStatus.PASS)
    )
    for index in range(1, 10):
        next_ticket = resumed.dispatch_next()
        assert next_ticket is not None and next_ticket.bootstrap_index == index
        row_route = _route(
            campaign_fingerprint=resumed.genesis.campaign_fingerprint,
            epoch=resumed.genesis.epoch,
            route_id=f"bootstrap-{index}",
            candidate=next_ticket.candidate,
            attempt_identity=next_ticket.attempt_identity,
        )
        row_evidence = _collect(row_route, _policy(), base_x=next_ticket.candidate.P)
        assert row_evidence.route == row_route
        safe_assessment = assess_attempt(
            next_ticket.candidate,
            AttemptDisposition.SAFE_NONTRAINABLE,
            qualification_passed=True,
        )
        assert safe_assessment.objective_value_n is None
        resumed = resumed.record_attempt(
            next_ticket,
            safe_assessment,
            return_receipt_sha256=f"{index + 20:064x}",
        )

    assert resumed.phase is CampaignPhase.READY_FOR_BO
    assert resumed.dispatch_next() is None
