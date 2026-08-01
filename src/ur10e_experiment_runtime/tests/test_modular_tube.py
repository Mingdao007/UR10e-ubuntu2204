from __future__ import annotations

from dataclasses import FrozenInstanceError
import inspect
import math
import time

import pytest

from ur10e_experiment_runtime.tube import (
    AcceptedCandidateConsumerV1,
    AcceptedCandidateProducerV1,
    CandidateBindingError,
    CircleSdfV1,
    CompositeSdfV1,
    DisabledTubeGuard,
    EoatProxyV1,
    EllipseSdfV1,
    ExclusionBoxConstraintV1,
    GeometryError,
    HalfSpaceConstraintV1,
    Pose3V1,
    ProgressGuardV1,
    ProgressReason,
    ProgressSampleV1,
    ProxyRegistryV1,
    ReferencePoseSampleV1,
    SignedDistanceGeometry,
    SpatialTubeGuard,
    SweptReferenceV1,
    TubePolicyV1,
    TubeReason,
    TubeState,
)


def pose(
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    quaternion: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
) -> Pose3V1:
    return Pose3V1(position_m=position, quaternion_xyzw=quaternion)


def reference() -> SweptReferenceV1:
    return SweptReferenceV1(
        samples=(ReferencePoseSampleV1("home", pose()),),
        reference_id="frozen-home",
    )


def proxy(
    extents: tuple[float, float, float] = (1e-6, 1e-6, 1e-6),
    *,
    transform: Pose3V1 | None = None,
    qualified: bool = True,
) -> EoatProxyV1:
    selected_transform = (
        transform if transform is not None else Pose3V1() if qualified else None
    )
    return EoatProxyV1(
        source_sha256="1" * 64,
        proxy_id="proxy:eoat-v4-obb",
        source_frame="base",
        half_extents_m=extents,
        T_tool_proxy=selected_transform,
        scope_exclusions=("unmodeled_cable", "unmodeled_sensor_mount"),
    )


def registered(proxy_value: EoatProxyV1) -> ProxyRegistryV1:
    return ProxyRegistryV1().register(proxy_value)


def policy_for(
    strategy: CircleSdfV1 | EllipseSdfV1 | CompositeSdfV1,
    proxy_value: EoatProxyV1,
    reference_value: SweptReferenceV1,
    **kwargs: object,
) -> TubePolicyV1:
    return TubePolicyV1.enabled_policy(
        strategy=strategy,
        proxy_sha256=proxy_value.sha256,
        reference_sha256=reference_value.sha256,
        warning_threshold_m=kwargs.pop("warning_threshold_m", 0.02),
        warning_dwell_ns=kwargs.pop("warning_dwell_ns", 0),
        hysteresis_m=kwargs.pop("hysteresis_m", 0.0),
        registration_sha256=kwargs.pop("registration_sha256", proxy_value.sha256),
        **kwargs,
    )


def test_proxy_registration_support_bounds_and_immutability() -> None:
    proxy_value = proxy(
        (0.1, 0.2, 0.3),
        transform=Pose3V1(
            position_m=(0.01, 0.0, 0.0),
            quaternion_xyzw=(0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)),
        ),
    )
    assert math.isclose(proxy_value.support_m((1.0, 0.0, 0.0)), 0.21)
    assert math.isclose(proxy_value.support_bound_m((1.0, 0.0, 0.0)), 0.21)
    assert not ProxyRegistryV1().contains(proxy_value)
    registry = ProxyRegistryV1().register(proxy_value)
    assert registry.contains(proxy_value)
    assert registry.resolve(proxy_value.sha256) == proxy_value
    with pytest.raises(FrozenInstanceError):
        proxy_value.proxy_id = "drifted"  # type: ignore[misc]


def test_circle_signed_field_reports_boundary_and_reference_identity() -> None:
    reference_value = reference()
    proxy_value = proxy()
    geometry = SignedDistanceGeometry(CircleSdfV1(0.1))

    inside = geometry.evaluate(pose(), proxy_value, reference_value)
    boundary = geometry.evaluate(pose((0.1, 0.0, 0.0)), proxy_value, reference_value)
    outside = geometry.evaluate(pose((0.11, 0.0, 0.0)), proxy_value, reference_value)

    assert inside.signed_distance_m < 0.0
    assert boundary.signed_distance_m == 0.0
    assert outside.signed_distance_m > 0.0
    assert inside.strategy == "circle_sdf_v1"
    assert inside.worst_constraint_id == "circle_sdf_v1"
    assert inside.reference_sample_id == "home"
    assert inside.reference_sample_identity == reference_value.samples[0].sha256


def test_ellipse_anisotropy_and_orientation_only_obb_breach() -> None:
    reference_value = reference()
    proxy_value = proxy((0.2, 0.01, 0.01))
    geometry = SignedDistanceGeometry(EllipseSdfV1(0.1, 0.2, 0.3))

    assert geometry.evaluate(pose((0.1, 0.0, 0.0)), proxy_value, reference_value).signed_distance_m == 0.0
    assert geometry.evaluate(pose((0.0, 0.2, 0.0)), proxy_value, reference_value).signed_distance_m == 0.0
    assert geometry.evaluate(pose((0.0, 0.0, 0.3)), proxy_value, reference_value).signed_distance_m == 0.0
    orientation_only = geometry.evaluate(
        pose(quaternion=(0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5))),
        proxy_value,
        reference_value,
    )
    assert orientation_only.signed_distance_m > 0.0

    circle_orientation_only = SignedDistanceGeometry(CircleSdfV1(0.1)).evaluate(
        pose(quaternion=(0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5))),
        proxy_value,
        reference_value,
    )
    assert circle_orientation_only.signed_distance_m > 0.0


def test_composite_is_max_intersection_and_rejects_space_expanding_operators() -> None:
    x_limit = HalfSpaceConstraintV1("x-limit", (1.0, 0.0, 0.0), 0.1)
    y_limit = HalfSpaceConstraintV1("y-limit", (0.0, 1.0, 0.0), 0.2)
    composite = CompositeSdfV1((x_limit, y_limit))
    reference_value = reference()
    proxy_value = proxy()
    geometry = SignedDistanceGeometry(composite)

    both_inside = geometry.evaluate(pose((-0.1, -0.1, 0.0)), proxy_value, reference_value)
    x_boundary = geometry.evaluate(pose((0.1, -0.1, 0.0)), proxy_value, reference_value)
    x_outside = geometry.evaluate(pose((0.3, -0.1, 0.0)), proxy_value, reference_value)
    assert both_inside.signed_distance_m < 0.0
    assert x_boundary.signed_distance_m == 0.0
    assert x_outside.signed_distance_m > 0.0
    assert x_outside.worst_constraint_id == "x-limit"

    exclusion = ExclusionBoxConstraintV1("keep-out", (-0.1, -0.1, -0.1), (0.1, 0.1, 0.1))
    exclusion_geometry = SignedDistanceGeometry(CompositeSdfV1((exclusion,)))
    assert exclusion_geometry.evaluate(pose(), proxy_value, reference_value).signed_distance_m > 0.0
    assert exclusion_geometry.evaluate(pose((0.2, 0.0, 0.0)), proxy_value, reference_value).signed_distance_m < 0.0

    with pytest.raises(GeometryError):
        CompositeSdfV1(())
    with pytest.raises(GeometryError):
        CompositeSdfV1((x_limit, x_limit))
    for forbidden in ("union", "negation", "subtraction"):
        with pytest.raises(GeometryError):
            CompositeSdfV1((x_limit,), operator=forbidden)


def test_disabled_guard_is_stateless_and_does_not_require_solver_inputs() -> None:
    assert TubeState.SAFE.value == "safe"
    policy = TubePolicyV1.disabled()
    assert policy.strategy is None
    assert policy.proxy_sha256 is None
    assert policy.reference_sha256 is None
    guard = SpatialTubeGuard(policy)
    assert isinstance(guard, DisabledTubeGuard)
    assert not hasattr(guard, "_warning_started_ns")
    first = guard.evaluate(None, object(), object(), now_ns=float("nan"))
    second = guard.evaluate("anything", sequence=object())
    assert first is second
    assert first.state is TubeState.DISABLED
    assert first.stop is False
    assert first.reason is TubeReason.DISABLED_BY_UNQUALIFIED_POLICY
    with pytest.raises(ValueError):
        TubePolicyV1(enabled=False, strategy=CircleSdfV1(0.1))


def test_spatial_guard_requires_exact_identity_and_registration() -> None:
    reference_value = reference()
    proxy_value = proxy()
    strategy = CircleSdfV1(0.1)
    policy = policy_for(strategy, proxy_value, reference_value)
    geometry = SignedDistanceGeometry(strategy)
    with pytest.raises(ValueError):
        SpatialTubeGuard(policy, geometry, ProxyRegistryV1())

    guard = SpatialTubeGuard(policy, geometry, registered(proxy_value))
    assert guard.evaluate(pose(), proxy_value, reference_value, now_ns=1).state is TubeState.SAFE
    other_proxy = EoatProxyV1(
        source_sha256="2" * 64,
        proxy_id="proxy:other",
        source_frame="base",
        half_extents_m=(1e-6, 1e-6, 1e-6),
        T_tool_proxy=Pose3V1(),
    )
    assert guard.evaluate(pose(), other_proxy, reference_value, now_ns=2).reason is TubeReason.PROXY_IDENTITY_MISMATCH
    other_reference = SweptReferenceV1((ReferencePoseSampleV1("other", pose()),), "other")
    assert guard.evaluate(pose(), proxy_value, other_reference, now_ns=3).reason is TubeReason.REFERENCE_IDENTITY_MISMATCH


def test_regression_none_tool_proxy_is_unqualified_and_cannot_activate() -> None:
    reference_value = reference()
    qualified_proxy = proxy()
    unqualified_proxy = proxy(qualified=False)
    geometry = SignedDistanceGeometry(CircleSdfV1(0.1))

    assert unqualified_proxy.T_tool_proxy is None
    with pytest.raises(ValueError):
        ProxyRegistryV1().register(unqualified_proxy)
    with pytest.raises(ValueError):
        ProxyRegistryV1((unqualified_proxy,))
    with pytest.raises(GeometryError):
        geometry.evaluate(pose(), unqualified_proxy, reference_value)

    policy = policy_for(CircleSdfV1(0.1), qualified_proxy, reference_value)
    guard = SpatialTubeGuard(policy, geometry, registered(qualified_proxy))
    decision = guard.evaluate(pose(), unqualified_proxy, reference_value, now_ns=0)
    assert decision.stop is True
    assert decision.reason is TubeReason.PROXY_UNREGISTERED

    unqualified_policy = policy_for(
        CircleSdfV1(0.1),
        unqualified_proxy,
        reference_value,
    )
    with pytest.raises(ValueError):
        SpatialTubeGuard(
            unqualified_policy,
            geometry,
            ProxyRegistryV1(),
        )


def test_regression_enabled_policy_requires_explicit_threshold_and_registration() -> None:
    reference_value = reference()
    proxy_value = proxy()
    strategy = CircleSdfV1(0.1)

    with pytest.raises(ValueError, match="warning_threshold_m"):
        TubePolicyV1(
            enabled=True,
            strategy=strategy,
            proxy_sha256=proxy_value.sha256,
            reference_sha256=reference_value.sha256,
            registration_sha256=proxy_value.sha256,
        )
    with pytest.raises(ValueError, match="registration_sha256"):
        TubePolicyV1(
            enabled=True,
            strategy=strategy,
            proxy_sha256=proxy_value.sha256,
            reference_sha256=reference_value.sha256,
            warning_threshold_m=0.02,
        )
    with pytest.raises(ValueError, match="registration_sha256"):
        policy_for(
            strategy,
            proxy_value,
            reference_value,
            registration_sha256="f" * 64,
        )


def test_regression_composite_accepts_pose_strategy_leaves_and_keeps_orientation() -> None:
    reference_value = reference()
    proxy_value = proxy((0.2, 0.01, 0.01))
    actual = pose(quaternion=(0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)))
    circle_child = CircleSdfV1(0.1, constraint_id="circle-child")
    ellipse_child = EllipseSdfV1(0.1, 0.5, 0.5, constraint_id="ellipse-child")

    circle_sample = SignedDistanceGeometry(circle_child).evaluate(
        actual,
        proxy_value,
        reference_value,
    )
    ellipse_sample = SignedDistanceGeometry(ellipse_child).evaluate(
        actual,
        proxy_value,
        reference_value,
    )
    composite_sample = SignedDistanceGeometry(
        CompositeSdfV1((circle_child, ellipse_child))
    ).evaluate(actual, proxy_value, reference_value)
    assert composite_sample.signed_distance_m == max(
        circle_sample.signed_distance_m,
        ellipse_sample.signed_distance_m,
    )
    assert composite_sample.signed_distance_m > 0.0
    assert composite_sample.worst_constraint_id == "circle-child"

    single_circle = SignedDistanceGeometry(CompositeSdfV1((circle_child,))).evaluate(
        actual,
        proxy_value,
        reference_value,
    )
    assert single_circle.signed_distance_m == circle_sample.signed_distance_m
    assert single_circle.worst_constraint_id == "circle-child"


def test_regression_source_hash_alias_and_strict_positive_obb_extents() -> None:
    source_hash = "2" * 64
    proxy_value = EoatProxyV1(
        source_sha256=source_hash,
        proxy_id="proxy:source-hash",
        source_frame="base",
        half_extents_m=(0.01, 0.02, 0.03),
        T_tool_proxy=Pose3V1(),
    )
    assert proxy_value.source_sha256 == source_hash
    assert proxy_value.source_id == source_hash
    assert proxy_value.proxy_sha256 == proxy_value.sha256
    assert proxy_value.canonical_document()["source_sha256"] == source_hash
    with pytest.raises(ValueError):
        EoatProxyV1(
            source_id="arbitrary-source-text",
            proxy_id="proxy:invalid-source",
            source_frame="base",
            half_extents_m=(0.01, 0.02, 0.03),
            T_tool_proxy=Pose3V1(),
        )
    for invalid_extents in ((0.0, 0.01, 0.01), (-0.01, 0.01, 0.01), (math.nan, 0.01, 0.01)):
        with pytest.raises(ValueError):
            EoatProxyV1(
                source_sha256=source_hash,
                proxy_id="proxy:invalid-extents",
                source_frame="base",
                half_extents_m=invalid_extents,
                T_tool_proxy=Pose3V1(),
            )


def test_warning_dwell_hysteresis_and_no_motion_command() -> None:
    reference_value = reference()
    proxy_value = proxy()
    strategy = CircleSdfV1(0.1)
    policy = policy_for(
        strategy,
        proxy_value,
        reference_value,
        warning_threshold_m=0.02,
        warning_dwell_ns=100,
        hysteresis_m=0.01,
    )
    guard = SpatialTubeGuard(policy, SignedDistanceGeometry(strategy), registered(proxy_value))
    near = pose((0.09, 0.0, 0.0))
    assert guard.evaluate(near, proxy_value, reference_value, now_ns=0).reason is TubeReason.WARNING_DWELL_PENDING
    assert guard.evaluate(near, proxy_value, reference_value, now_ns=99).state is TubeState.SAFE
    warning = guard.evaluate(near, proxy_value, reference_value, now_ns=100)
    assert warning.state is TubeState.WARNING
    assert warning.stop is False
    assert warning.telemetry_only is True
    assert warning.motion_command is None
    assert guard.evaluate(pose((0.075, 0.0, 0.0)), proxy_value, reference_value, now_ns=101).state is TubeState.WARNING
    cleared = guard.evaluate(pose((0.069, 0.0, 0.0)), proxy_value, reference_value, now_ns=102)
    assert cleared.state is TubeState.SAFE
    assert cleared.reason is TubeReason.WARNING_CLEARED


def test_stop_is_immediate_at_boundary_and_invalid_inputs_fail_closed() -> None:
    reference_value = reference()
    proxy_value = proxy()
    strategy = CircleSdfV1(0.1)
    policy = policy_for(strategy, proxy_value, reference_value, warning_dwell_ns=10_000)
    guard = SpatialTubeGuard(policy, SignedDistanceGeometry(strategy), registered(proxy_value))
    boundary = guard.evaluate(pose((0.1, 0.0, 0.0)), proxy_value, reference_value, now_ns=0)
    assert boundary.stop is True
    assert boundary.reason is TubeReason.STOP_BOUNDARY
    assert guard.evaluate(None, proxy_value, reference_value, now_ns=1).stop is True
    assert guard.evaluate(pose(), proxy_value, reference_value).reason is TubeReason.MISSING_DWELL_TIME
    assert guard.evaluate(object(), proxy_value, reference_value, now_ns=2).stop is True
    assert guard.evaluate(pose(), proxy_value, reference_value, now_ns=1).reason is TubeReason.NONMONOTONIC_DWELL_CLOCK
    with pytest.raises(GeometryError):
        SignedDistanceGeometry(strategy).evaluate(
            Pose3V1(frame_id="other"),
            proxy_value,
            reference_value,
        )


def test_spatial_geometry_is_decoupled_from_progress_and_time() -> None:
    signature = inspect.signature(SignedDistanceGeometry.evaluate)
    assert tuple(signature.parameters) == ("self", "actual_pose", "proxy", "swept_reference")
    reference_value = reference()
    proxy_value = proxy()
    geometry = SignedDistanceGeometry(CircleSdfV1(0.1))
    first = geometry.evaluate(pose((0.01, 0.0, 0.0)), proxy_value, reference_value)
    second = geometry.evaluate(pose((0.01, 0.0, 0.0)), proxy_value, reference_value)
    assert first == second
    assert ProgressSampleV1(1, 1, freshness_age_ns=0, lag_ns=0) != ProgressSampleV1(2, 2)


def test_progress_guard_has_typed_independent_failure_reasons() -> None:
    guard = ProgressGuardV1(max_freshness_age_ns=10, max_lag_ns=20, freeze_dwell_ns=100)
    assert guard.evaluate(ProgressSampleV1(1, 100, 0, 0, 0)).reason is ProgressReason.OK
    assert guard.evaluate(ProgressSampleV1(1, 100, 0, 0, 50)).reason is ProgressReason.FREEZE_PENDING
    frozen = guard.evaluate(ProgressSampleV1(1, 100, 0, 0, 100))
    assert frozen.stop is True
    assert frozen.reason is ProgressReason.FREEZE

    guard.reset()
    assert guard.evaluate(ProgressSampleV1(2, 200, 0, 0)).reason is ProgressReason.OK
    assert guard.evaluate(ProgressSampleV1(1, 201, 0, 0)).reason is ProgressReason.SEQUENCE
    guard.reset()
    assert guard.evaluate(ProgressSampleV1(2, 200, 0, 0)).reason is ProgressReason.OK
    assert guard.evaluate(ProgressSampleV1(3, 199, 0, 0)).reason is ProgressReason.TIMESTAMP
    guard.reset()
    assert guard.evaluate(ProgressSampleV1(1, 1, 11, 0)).reason is ProgressReason.FRESHNESS
    guard.reset()
    assert guard.evaluate(ProgressSampleV1(1, 1, 0, 21)).reason is ProgressReason.LAG
    assert guard.evaluate(None).reason is ProgressReason.MISSING_PROGRESS


def test_accepted_candidate_seal_consume_and_hash_bindings() -> None:
    reference_value = reference()
    proxy_value = proxy()
    policy = policy_for(CircleSdfV1(0.1), proxy_value, reference_value)
    hashes = {
        "campaign": "a" * 64,
        "ledger": "b" * 64,
        "eoat": "c" * 64,
        "source_a": "d" * 64,
        "source_b": "e" * 64,
    }
    producer = AcceptedCandidateProducerV1()
    candidate = producer.seal(
        accepted=True,
        candidate_uid="candidate-001",
        parameters={"gain": 1.5, "damping": 0.2},
        campaign_fingerprint=hashes["campaign"],
        ledger_root=hashes["ledger"],
        eoat_profile=hashes["eoat"],
        proxy=proxy_value,
        tube_policy=policy,
        source_closure_hashes=(hashes["source_b"], hashes["source_a"]),
    )
    assert dict(candidate.parameters) == {"damping": 0.2, "gain": 1.5}
    with pytest.raises(TypeError):
        candidate.parameters["gain"] = 2.0  # type: ignore[index]
    consumer = AcceptedCandidateConsumerV1(
        candidate_uid="candidate-001",
        campaign_fingerprint=hashes["campaign"],
        ledger_root=hashes["ledger"],
        eoat_profile=hashes["eoat"],
        proxy=proxy_value,
        tube_policy=policy,
        source_closure_hashes=(hashes["source_a"], hashes["source_b"]),
    )
    assert consumer.consume(candidate) == candidate
    artifact = candidate.to_artifact(include_canonical_json=True)
    assert consumer.consume(artifact).canonical_sha256 == candidate.canonical_sha256

    drifted = dict(artifact)
    drifted["parameters"] = {"damping": 0.3, "gain": 1.5}
    with pytest.raises((CandidateBindingError, ValueError)):
        consumer.consume(drifted)
    noncanonical = dict(artifact)
    noncanonical["canonical_json"] = noncanonical["canonical_json"] + " "
    with pytest.raises((CandidateBindingError, ValueError)):
        consumer.consume(noncanonical)
    with pytest.raises(CandidateBindingError):
        producer.seal(
            accepted=False,
            candidate_uid="candidate-002",
            parameters={"gain": 1.0},
            campaign_fingerprint=hashes["campaign"],
            ledger_root=hashes["ledger"],
            eoat_profile=hashes["eoat"],
            proxy=proxy_value,
            tube_policy=policy,
            source_closure_hashes=(hashes["source_a"],),
        )
    with pytest.raises(CandidateBindingError):
        producer.seal(
            accepted=True,
            candidate_uid="candidate-003",
            parameters={"gain": math.inf},
            campaign_fingerprint=hashes["campaign"],
            ledger_root=hashes["ledger"],
            eoat_profile=hashes["eoat"],
            proxy=proxy_value,
            tube_policy=policy,
            source_closure_hashes=(hashes["source_a"],),
        )


def test_repeated_geometry_is_deterministic_and_above_460_evaluations_per_second(capsys: pytest.CaptureFixture[str]) -> None:
    reference_value = reference()
    proxy_value = proxy((0.01, 0.01, 0.01))
    geometry = SignedDistanceGeometry(CircleSdfV1(0.1))
    actual = pose((0.01, -0.02, 0.01))
    first = geometry.evaluate(actual, proxy_value, reference_value)
    for _ in range(10):
        assert geometry.evaluate(actual, proxy_value, reference_value) == first

    iterations = 2_000
    started = time.perf_counter()
    for _ in range(iterations):
        geometry.evaluate(actual, proxy_value, reference_value)
    elapsed = time.perf_counter() - started
    evaluations_per_second = iterations / elapsed
    print(f"modular tube diagnostic: {evaluations_per_second:.0f} evaluations/s")
    assert evaluations_per_second > 460.0
