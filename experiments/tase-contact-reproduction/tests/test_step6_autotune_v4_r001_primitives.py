"""Golden tests for the offline-only Step6 Figure-Eight r001 primitives."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from step6_autotune_v4_r001 import (
    CENTER_CROSSING_WINDOW_END_S,
    CENTER_CROSSING_WINDOW_START_S,
    FORCE_MAE_V1_COMPAT_SHADOW_ID,
    FORCE_MAE_V1_COMPAT_SHADOW_ROLE,
    FORCE_MAE_V2_SPEC,
    FORCE_MAE_V2_SPEC_FINGERPRINT,
    FORCE_MAE_V2_SPEC_ID,
    FORCE_MAE_V2_VERSION,
    FORMAL_BIN_COUNT,
    FORMAL_WINDOW_END_S,
    FORMAL_WINDOW_START_S,
    HOME_CARTESIAN_POSE,
    HUMAN_LABEL,
    IncompleteCoverageError,
    InvalidForceSampleError,
    InvalidPathTimeError,
    LOBE_CLASSIFICATION_RULE,
    MetricRole,
    AutotuneTaskProfile,
    ContractInvariantError,
    ForceMaeSpec,
    PROGRAM_NAME,
    PROFILE,
    PathReference,
    REVISION,
    SAFE_FRAME_SHA256,
    SAFE_FRAME_SOURCE,
    SafeFrameValidationError,
    STEP6_R001_CONTRACT,
    STEP6_R001_PROFILE,
    Step6R001Contract,
    Step6PrimitiveError,
    THEORETICAL_PEAK_SPEED_M_S,
    DuplicateIdentityConflictError,
    ForceSample,
    ForceSampleIdentity,
    InheritedXYFrame,
    compute_force_metrics,
    load_inherited_xy_frame,
)


def _reference() -> PathReference:
    return PathReference.from_inherited_source()


def _bin_time(index: int) -> float:
    return float(Decimal("5.05") + Decimal("0.1") * index)


def _sample(sequence: int, path_time_s: float, force_n: float = 5.0, source_id: str = "golden") -> ForceSample:
    return ForceSample(
        path_time_s=path_time_s,
        filtered_normal_n=force_n,
        sample_identity=ForceSampleIdentity(source_id=source_id, sequence=sequence),
    )


def _complete_evidence(force_for_bin=None) -> list[ForceSample]:
    if force_for_bin is None:
        force_for_bin = lambda _index: 5.0
    return [
        _sample(index, _bin_time(index), float(force_for_bin(index)))
        for index in range(FORMAL_BIN_COUNT)
    ]


def test_identity_home_program_and_inherited_frame_hash_are_locked() -> None:
    contract = STEP6_R001_CONTRACT
    assert contract.lineage == "step6_strict_rnn_autotune_v4"
    assert contract.profile == PROFILE == "autotune_v4.step6_figure8_paper"
    assert contract.revision == REVISION == "r001"
    assert contract.program_name == PROGRAM_NAME == "step6_strict_rnn_autotune_v4_r001"
    assert contract.human_label == HUMAN_LABEL == "Autotune V4 / Step6 Figure-Eight (Paper 60s)"
    assert contract.home_cartesian_pose == HOME_CARTESIAN_POSE == (
        0.462055182,
        0.177882596,
        0.033,
        3.120752062,
        0.0,
        0.068626833,
    )
    assert contract.home_q is None
    assert contract.safe_frame_source == SAFE_FRAME_SOURCE
    frame = load_inherited_xy_frame()
    assert frame.source_sha256 == SAFE_FRAME_SHA256
    assert frame.source_path.endswith("config/step6_eight_safe_frame.json")
    assert math.isclose(
        frame.u_along_xy[0] * frame.p_lateral_xy[1]
        - frame.u_along_xy[1] * frame.p_lateral_xy[0],
        1.0,
        abs_tol=1e-12,
    )


def test_autotune_profile_and_force_mae_spec_are_single_typed_locked_seams() -> None:
    profile = STEP6_R001_PROFILE
    spec = FORCE_MAE_V2_SPEC
    assert isinstance(profile, AutotuneTaskProfile)
    assert Step6R001Contract is AutotuneTaskProfile
    assert isinstance(spec, ForceMaeSpec)
    assert profile is STEP6_R001_CONTRACT
    assert profile.__dataclass_fields__["mae_spec_id"].init is False
    assert profile.force_mae_spec is spec
    assert profile.mae_spec_id == "force_mae_v2" == FORCE_MAE_V2_SPEC_ID
    assert spec.version == FORCE_MAE_V2_VERSION == "force_mae_v2"
    assert spec.mae_spec_id == "force_mae_v2"
    assert spec.target_force_n == spec.target_n == 5.0
    assert (spec.window_start_s, spec.window_end_s) == (5.0, 60.0)
    assert spec.bin_width_s == 0.1
    assert spec.bin_count == 550
    assert spec.requires_complete_coverage is True
    assert spec.requires_distinct_sample_identities is True
    assert spec.requires_distinct_identities is True
    assert spec.v1_shadow_id == FORCE_MAE_V1_COMPAT_SHADOW_ID
    assert spec.v1_shadow_role == FORCE_MAE_V1_COMPAT_SHADOW_ROLE == "audit_only"
    assert spec.v2_aggregation_order == "sample_abs_then_bin_mean_then_equal_bin_mean"
    assert spec.v1_shadow_aggregation_order == "bin_mean_then_abs_then_equal_bin_mean"
    assert spec.semantic_fingerprint == spec.spec_fingerprint
    assert profile.semantic_fingerprint == profile.spec_fingerprint == profile.mae_spec_fingerprint
    assert spec.semantic_fingerprint == profile.semantic_fingerprint
    assert FORCE_MAE_V2_SPEC_FINGERPRINT == "a9a1baa17cdb8b3ccc611505e88bb052b51271db1d93691ffdf553c9586f74ec"
    assert len(spec.semantic_fingerprint) == 64
    assert int(spec.semantic_fingerprint, 16) >= 0
    assert replace(spec).semantic_fingerprint == spec.semantic_fingerprint
    assert profile.force_target_n == spec.target_force_n
    assert _reference().profile is profile

    with pytest.raises(ContractInvariantError):
        replace(spec, target_force_n=4.0)
    with pytest.raises(ContractInvariantError):
        replace(spec, version="force_mae_v1")
    with pytest.raises(ContractInvariantError):
        replace(spec, window_start_s=4.0)
    with pytest.raises(ContractInvariantError):
        replace(spec, bin_count=549)
    with pytest.raises(ContractInvariantError):
        replace(spec, requires_complete_coverage=False)
    with pytest.raises(ContractInvariantError):
        replace(spec, requires_distinct_sample_identities=False)
    with pytest.raises(ContractInvariantError):
        replace(spec, v1_shadow_role="optimizer_objective")
    with pytest.raises(ContractInvariantError):
        replace(spec, v2_aggregation_order="bin_mean_then_abs")
    with pytest.raises(ContractInvariantError):
        replace(spec, v2_sample_error_operation="bin_mean_then_abs")
    with pytest.raises(ContractInvariantError):
        replace(profile, program_z_delta_m=0.0347)
    with pytest.raises(FrozenInstanceError):
        profile.mae_spec_id = "other"  # type: ignore[misc]


def test_figure_eight_formula_derivatives_endpoints_and_phase() -> None:
    reference = _reference()
    at_zero = reference.evaluate(0.0)
    assert at_zero.path_time_s == 0.0
    assert at_zero.phase_rad == 0.0
    assert at_zero.along_m == pytest.approx(0.0)
    assert at_zero.lateral_m == pytest.approx(0.0)
    assert at_zero.along_derivative_m_s == pytest.approx(0.004)
    assert at_zero.lateral_derivative_m_s == pytest.approx(0.002)
    assert at_zero.reference_speed_m_s == pytest.approx(math.hypot(0.004, 0.002))

    at_sixty = reference.evaluate(60.0)
    assert at_sixty.path_time_s == 60.0
    assert at_sixty.phase_rad == pytest.approx(6.0)
    assert at_sixty.along_m == pytest.approx(0.04 * math.sin(6.0))
    assert at_sixty.lateral_m == pytest.approx(0.01 * math.sin(12.0))
    assert at_sixty.along_derivative_m_s == pytest.approx(0.004 * math.cos(6.0))
    assert at_sixty.lateral_derivative_m_s == pytest.approx(0.002 * math.cos(12.0))
    assert not math.isclose(at_sixty.along_m, 0.0, abs_tol=1e-12)
    assert not math.isclose(at_sixty.lateral_m, 0.0, abs_tol=1e-12)


def test_peak_speed_guard_and_tangential_command_cap_are_distinct() -> None:
    reference = _reference()
    at_zero = reference.evaluate(0.0)
    peak = math.hypot(at_zero.along_derivative_m_s, at_zero.lateral_derivative_m_s)
    assert peak == pytest.approx(THEORETICAL_PEAK_SPEED_M_S, abs=1e-18)
    assert THEORETICAL_PEAK_SPEED_M_S == pytest.approx(0.00447213595499958, abs=1e-18)
    assert reference.reference_speed_guard_m_s == 0.0045
    assert reference.tangential_command_cap_m_s == 0.010
    assert peak <= reference.reference_speed_guard_m_s
    assert reference.reference_speed_guard_m_s <= reference.tangential_command_cap_m_s


def test_local_to_base_xy_and_analytical_velocity_transform_use_inherited_basis() -> None:
    frame = load_inherited_xy_frame()
    reference = PathReference(frame)
    sample = reference.evaluate(17.25)
    expected_xy = tuple(
        frame.origin_xy_m[coordinate]
        + sample.along_m * frame.u_along_xy[coordinate]
        + sample.lateral_m * frame.p_lateral_xy[coordinate]
        for coordinate in range(2)
    )
    expected_velocity = tuple(
        sample.along_derivative_m_s * frame.u_along_xy[coordinate]
        + sample.lateral_derivative_m_s * frame.p_lateral_xy[coordinate]
        for coordinate in range(2)
    )
    assert sample.desired_base_xy_m == pytest.approx(expected_xy, abs=1e-15)
    assert sample.analytical_base_xy_velocity_m_s == pytest.approx(expected_velocity, abs=1e-15)
    assert sample.reference_speed_m_s == pytest.approx(
        math.hypot(sample.along_derivative_m_s, sample.lateral_derivative_m_s),
        abs=1e-15,
    )
    assert math.hypot(*sample.analytical_base_xy_velocity_m_s) == pytest.approx(
        sample.reference_speed_m_s,
        abs=1e-12,
    )
    with pytest.raises(ContractInvariantError):
        replace(sample, reference_speed_m_s=0.0)
    with pytest.raises(ContractInvariantError):
        replace(sample, reference_speed_m_s=float("nan"))
    assert sample.base_xy_m == sample.desired_base_xy_m
    assert sample.base_velocity_xy_m_s == sample.analytical_base_xy_velocity_m_s


def test_zero_program_z_delta_never_adds_34_7_mm_to_path() -> None:
    reference = _reference()
    sample = reference.evaluate(23.0)
    assert STEP6_R001_CONTRACT.program_z_delta_m == 0.0
    assert reference.program_z_delta_m == 0.0
    assert not hasattr(sample, "desired_base_z_m")
    assert not hasattr(sample, "desired_base_pose")


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), -0.001, 60.000001])
def test_invalid_path_times_fail_closed(bad_time: float) -> None:
    with pytest.raises(InvalidPathTimeError):
        _reference().evaluate(bad_time)


def test_invalid_source_geometry_and_digest_fail_closed() -> None:
    with pytest.raises(SafeFrameValidationError):
        InheritedXYFrame(
            source_path="config/step6_eight_safe_frame.json",
            source_sha256=SAFE_FRAME_SHA256,
            origin_xy_m=(0.0, 0.0),
            u_along_xy=(float("nan"), 1.0),
            p_lateral_xy=(-1.0, 0.0),
        )
    with pytest.raises(SafeFrameValidationError):
        load_inherited_xy_frame(expected_sha256="0" * 64)


def test_v2_uses_per_sample_absolute_error_and_v1_shadow_keeps_legacy_cancellation() -> None:
    samples = _complete_evidence()
    samples.extend(
        [
            _sample(FORMAL_BIN_COUNT, _bin_time(0), 6.0),
            _sample(FORMAL_BIN_COUNT + 1, _bin_time(0), 4.0),
        ]
    )
    metrics = compute_force_metrics(samples)
    assert metrics.force_mae_v2_n == pytest.approx((2.0 / 3.0) / FORMAL_BIN_COUNT)
    assert metrics.force_mae_v1_compat_shadow_n == pytest.approx(0.0)
    assert metrics.bins[0].distinct_sample_count == 3
    assert metrics.bins[0].v2_abs_error_mean_n == pytest.approx(2.0 / 3.0)
    assert metrics.bins[0].v1_compat_abs_error_n == pytest.approx(0.0)
    assert metrics.v2_role is MetricRole.OPTIMIZER_OBJECTIVE
    assert metrics.v1_role is MetricRole.AUDIT_ONLY
    assert metrics.force_mae_v1_compat_n == metrics.force_mae_v1_compat_shadow_n
    assert metrics.mae_spec_id == "force_mae_v2"
    assert metrics.coverage.mae_spec_id == "force_mae_v2"


def test_step6_same_bin_plus_minus_one_has_v2_one_and_v1_zero() -> None:
    samples = _complete_evidence()[1:]
    samples.extend([_sample(550, _bin_time(0), 6.0), _sample(551, _bin_time(0), 4.0)])
    metrics = compute_force_metrics(samples)
    assert metrics.bins[0].distinct_sample_count == 2
    assert metrics.bins[0].v2_abs_error_mean_n == pytest.approx(1.0)
    assert metrics.bins[0].v1_compat_abs_error_n == pytest.approx(0.0)
    assert metrics.force_mae_v2_n == pytest.approx(1.0 / FORMAL_BIN_COUNT)
    assert metrics.force_mae_v1_compat_shadow_n == pytest.approx(0.0)
    assert metrics.semantic_fingerprint == FORCE_MAE_V2_SPEC_FINGERPRINT
    assert metrics.spec_fingerprint == metrics.mae_spec_fingerprint == metrics.semantic_fingerprint
    assert metrics.coverage.semantic_fingerprint == FORCE_MAE_V2_SPEC_FINGERPRINT
    assert metrics.coverage.spec_fingerprint == metrics.coverage.mae_spec_fingerprint


def test_formal_window_excludes_first_five_seconds_and_includes_final_segment() -> None:
    samples = _complete_evidence(lambda index: 5.5 if index >= 500 else 5.0)
    samples.extend([_sample(600, 4.999, 105.0), _sample(601, 60.0, -95.0)])
    metrics = compute_force_metrics(samples)
    assert metrics.force_mae_v2_n == pytest.approx((50 * 0.5) / FORMAL_BIN_COUNT)
    assert metrics.coverage.pre_window_distinct_sample_count == 1
    assert metrics.coverage.end_boundary_distinct_sample_count == 1
    assert metrics.coverage.in_window_distinct_sample_count == FORMAL_BIN_COUNT
    assert metrics.coverage.per_bin_distinct_sample_counts[-1] == 1
    assert metrics.diagnostics.segment_windows[-1].start_s == 55.0
    assert metrics.diagnostics.segment_windows[-1].end_s == 60.0
    assert metrics.diagnostics.segment_maes_n[-1] == pytest.approx(0.5)


def test_exact_five_and_sixty_boundaries_are_half_open_as_specified() -> None:
    samples = _complete_evidence()
    samples.extend([_sample(700, 5.0, 6.0), _sample(701, 60.0, 500.0)])
    metrics = compute_force_metrics(samples)
    assert metrics.bins[0].window.start_s == FORMAL_WINDOW_START_S
    assert metrics.bins[0].distinct_sample_count == 2
    assert metrics.bins[0].v2_abs_error_mean_n == pytest.approx(0.5)
    assert metrics.coverage.end_boundary_distinct_sample_count == 1
    assert metrics.coverage.per_bin_distinct_sample_counts[-1] == 1


def test_exact_replay_is_deduplicated_and_conflicting_identity_is_rejected() -> None:
    samples = _complete_evidence()
    replay = samples[17]
    metrics = compute_force_metrics(samples + [replay])
    assert metrics.coverage.total_input_sample_count == FORMAL_BIN_COUNT + 1
    assert metrics.coverage.distinct_sample_count == FORMAL_BIN_COUNT
    assert metrics.coverage.exact_replay_count == 1
    assert metrics.coverage.per_bin_distinct_sample_counts[17] == 1

    conflict = _sample(replay.sample_identity.sequence, replay.path_time_s, 6.0)
    with pytest.raises(DuplicateIdentityConflictError):
        compute_force_metrics(samples + [conflict])


def test_missing_formal_bin_produces_no_objective() -> None:
    with pytest.raises(IncompleteCoverageError) as error:
        compute_force_metrics(_complete_evidence()[1:])
    assert error.value.missing_bin_indices == (0,)
    assert error.value.observed_bin_sample_counts[0] == 0


def test_diagnostics_use_equal_weighted_bin_level_v2_values_and_fixed_windows() -> None:
    def diagnostic_force(index: int) -> float:
        if index < 264:
            return 6.0  # right lobe, phase interval entirely before pi
        if index == 264:
            return 7.0  # the deterministic center-straddling bin is ambiguous
        return 8.0  # left lobe, phase interval entirely after pi

    metrics = compute_force_metrics(_complete_evidence(diagnostic_force))
    diagnostics = metrics.diagnostics
    assert diagnostics.right_lobe_mae_n == pytest.approx(1.0)
    assert diagnostics.left_lobe_mae_n == pytest.approx(3.0)
    assert diagnostics.lobe_imbalance_n == pytest.approx(2.0)
    assert diagnostics.ambiguous_center_bin_indices == (264,)
    assert diagnostics.lobe_classification_rule == LOBE_CLASSIFICATION_RULE
    assert diagnostics.center_crossing_window.start_s == CENTER_CROSSING_WINDOW_START_S
    assert diagnostics.center_crossing_window.end_s == CENTER_CROSSING_WINDOW_END_S
    assert diagnostics.center_crossing_mae_n == pytest.approx(2.42)
    assert len(diagnostics.segment_windows) == 11
    assert [window.start_s for window in diagnostics.segment_windows] == [
        5.0,
        10.0,
        15.0,
        20.0,
        25.0,
        30.0,
        35.0,
        40.0,
        45.0,
        50.0,
        55.0,
    ]
    assert diagnostics.segment_maes_n[5] == pytest.approx(2.42)
    assert diagnostics.worst_segment_index == 6
    assert diagnostics.worst_segment_window.start_s == 35.0
    assert diagnostics.worst_segment_window.end_s == 40.0
    assert diagnostics.worst_segment_mae_n == pytest.approx(3.0)
    assert metrics.force_mae_v2_n == pytest.approx(
        (264 * 1.0 + 1 * 2.0 + 285 * 3.0) / FORMAL_BIN_COUNT
    )


@pytest.mark.parametrize(
    "bad_sample",
    [
        (float("nan"), 5.0),
        (float("inf"), 5.0),
        (5.0, float("nan")),
        (5.0, float("inf")),
        (-1.0, 5.0),
        (60.001, 5.0),
    ],
)
def test_invalid_force_sample_time_or_force_is_rejected(bad_sample: tuple[float, float]) -> None:
    with pytest.raises(InvalidForceSampleError):
        _sample(1, bad_sample[0], bad_sample[1])


def test_force_sample_identity_is_required_and_primitives_are_typed() -> None:
    with pytest.raises(InvalidForceSampleError):
        ForceSample(path_time_s=5.0, filtered_normal_n=5.0, sample_identity="not-stable-typed")  # type: ignore[arg-type]
    assert isinstance(_complete_evidence()[0], ForceSample)
    assert isinstance(_complete_evidence()[0].sample_identity, ForceSampleIdentity)
    with pytest.raises(Step6PrimitiveError):
        ForceSampleIdentity(source_id="", sequence=0)
