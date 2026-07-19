from __future__ import annotations

from ur10e_experiment_runtime.moving_sphere import (
    MovingSphereKernel,
    SphereReason,
    StoppingBoundArtifact,
)
from ur10e_experiment_runtime.stage_adapters import (
    ControllerProgress,
    ControllerProgressPhase,
    PATH_ORIGIN_XY_M,
    Stage25ControllerProgressAdapter,
)


REF = "a" * 64


def certified() -> StoppingBoundArtifact:
    return StoppingBoundArtifact(
        reaction_latency_s=0.002,
        acceleration_growth_m_s2=0.1,
        minimum_deceleration_m_s2=2.0,
        center_speed_bound_m_s=0.002,
        center_acceleration_bound_m_s2=0.001,
        numeric_margin_m=0.0001,
        evidence_sha256=("b" * 64,),
        validity_domain="offline_fixture_only",
        certified=True,
    )


def active_progress(kernel: MovingSphereKernel, **overrides) -> ControllerProgress:
    sequence = kernel.last_controller_tick_seq + 1
    timestamp_ns = kernel.last_controller_timestamp_ns + 2_000_000
    values = {
        "phase": ControllerProgressPhase.ACTIVE_STAGE25,
        "controller_tick_seq": sequence,
        "controller_timestamp_ns": timestamp_ns,
        "age_ns": 2_000_000,
        "progress_s": 0.0,
        "center_x_m": 0.0,
        "center_y_m": 0.0,
        "center_z_m": 0.0,
        "reference_sha256": REF,
        "monotonic": True,
        "center_frozen": False,
    }
    values.update(overrides)
    return ControllerProgress(**values)


def tick(kernel: MovingSphereKernel, **overrides):
    progress_overrides = overrides.pop("progress_overrides", {})
    values = {
        "progress": active_progress(kernel, **progress_overrides),
        "tcp_base": (0.0, 0.0, 0.0),
        "tcp_speed_m_s": 0.0,
    }
    values.update(overrides)
    return kernel.tick(**values)


def test_actual_and_predicted_breaches_have_distinct_reasons() -> None:
    kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=certified())
    assert tick(kernel, tcp_base=(0.016, 0.0, 0.0)).reason is SphereReason.SPHERE_ACTUAL_BREACH
    result = tick(kernel, tcp_base=(0.014, 0.0, 0.0), tcp_speed_m_s=0.1)
    assert result.reason is SphereReason.SPHERE_PREDICTED_STOP_BREACH


def test_missing_nonfinite_reference_stale_and_uncertified_fail_closed() -> None:
    kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=certified())
    assert kernel.tick(progress=None, tcp_base=None, tcp_speed_m_s=None).reason is SphereReason.SPHERE_INPUT_MISSING
    assert tick(kernel, tcp_speed_m_s=float("nan")).reason is SphereReason.SPHERE_INPUT_NONFINITE
    assert tick(
        kernel,
        progress_overrides={"reference_sha256": "c" * 64},
    ).reason is SphereReason.SPHERE_REFERENCE_MISMATCH
    assert tick(
        kernel,
        progress_overrides={"age_ns": 2_000_001},
    ).reason is SphereReason.SPHERE_PROGRESS_STALE
    kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=None)
    assert tick(kernel).reason is SphereReason.SPHERE_STOP_BOUND_UNCERTIFIED


def test_certified_bound_outside_required_live_domain_fails_closed() -> None:
    kernel = MovingSphereKernel(
        reference_sha256=REF,
        stopping_bound=certified(),
        required_validity_domain="exact_live_domain",
    )
    result = tick(kernel, tcp_base=(0.0, 0.0, 0.0))
    assert result.stop is True
    assert result.reason is SphereReason.SPHERE_STOP_BOUND_DOMAIN_MISMATCH


def test_certified_bound_must_cover_explicit_tp_watchdog_latency() -> None:
    kernel = MovingSphereKernel(
        reference_sha256=REF,
        stopping_bound=certified(),
        minimum_reaction_latency_s=0.020,
    )
    result = tick(kernel, tcp_base=(0.0, 0.0, 0.0))
    assert result.stop is True
    assert result.reason is SphereReason.SPHERE_STOP_BOUND_LATENCY_INSUFFICIENT


def test_only_enumerated_inactive_phase_bypasses_sphere() -> None:
    kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=certified())
    inactive = ControllerProgress(phase=ControllerProgressPhase.INACTIVE)
    assert not kernel.tick(progress=inactive, tcp_base=None, tcp_speed_m_s=None).stop
    unknown = ControllerProgress(phase=ControllerProgressPhase.UNKNOWN)
    assert kernel.tick(progress=unknown, tcp_base=None, tcp_speed_m_s=None).reason is SphereReason.SPHERE_STAGE_UNKNOWN


def test_repeated_or_regressing_controller_sample_fails_closed() -> None:
    kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=certified())
    first = active_progress(kernel)
    assert kernel.tick(progress=first, tcp_base=(0.0, 0.0, 0.0), tcp_speed_m_s=0.0).reason is SphereReason.SPHERE_OK
    assert kernel.tick(progress=first, tcp_base=(0.0, 0.0, 0.0), tcp_speed_m_s=0.0).reason is SphereReason.SPHERE_PROGRESS_NONSEQUENTIAL


def test_adapter_owns_stage_classification_reference_and_frozen_center() -> None:
    adapter = Stage25ControllerProgressAdapter(physical_prior_sha256="d" * 64)
    inactive = adapter.sample(
        stage=24.2,
        controller_progress_s=0.0,
        controller_tick_seq=500,
        controller_timestamp_s=1.0,
        age_ns=0,
        tcp_z_m=0.01,
    )
    assert inactive.phase is ControllerProgressPhase.INACTIVE
    unknown = adapter.sample(
        stage=99.0,
        controller_progress_s=0.0,
        controller_tick_seq=501,
        controller_timestamp_s=1.002,
        age_ns=0,
        tcp_z_m=0.01,
    )
    assert unknown.phase is ControllerProgressPhase.UNKNOWN
    first = adapter.sample(
        stage=25.0,
        controller_progress_s=0.0,
        controller_tick_seq=502,
        controller_timestamp_s=1.004,
        age_ns=0,
        tcp_z_m=0.008,
    )
    first_center = (first.center_x_m, first.center_y_m, first.center_z_m)
    first_sequence = first.controller_tick_seq
    assert first_center == (*PATH_ORIGIN_XY_M, 0.008)
    second = adapter.sample(
        stage=25.0,
        controller_progress_s=0.0,
        controller_tick_seq=503,
        controller_timestamp_s=1.006,
        age_ns=0,
        tcp_z_m=0.009,
    )
    assert second.center_frozen is True
    assert (second.center_x_m, second.center_y_m, second.center_z_m) == first_center
    assert second.controller_tick_seq == first_sequence + 1


def test_adapter_and_kernel_reject_skipped_controller_tick() -> None:
    adapter = Stage25ControllerProgressAdapter(physical_prior_sha256="d" * 64)
    kernel = MovingSphereKernel(
        reference_sha256=adapter.reference_sha256,
        stopping_bound=certified(),
    )
    first = adapter.sample(
        stage=25.0,
        controller_progress_s=0.0,
        controller_tick_seq=500,
        controller_timestamp_s=1.0,
        age_ns=0,
        tcp_z_m=0.008,
    )
    assert kernel.tick(
        progress=first,
        tcp_base=(first.center_x_m, first.center_y_m, first.center_z_m),
        tcp_speed_m_s=0.0,
    ).reason is SphereReason.SPHERE_OK
    jumped = adapter.sample(
        stage=25.0,
        controller_progress_s=0.01,
        controller_tick_seq=505,
        controller_timestamp_s=1.01,
        age_ns=0,
        tcp_z_m=0.008,
    )
    assert kernel.tick(
        progress=jumped,
        tcp_base=(jumped.center_x_m, jumped.center_y_m, jumped.center_z_m),
        tcp_speed_m_s=0.0,
    ).reason is SphereReason.SPHERE_PROGRESS_NONSEQUENTIAL


def test_active_bound_remains_conservative_when_center_is_frozen() -> None:
    moving_kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=certified())
    frozen_kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=certified())
    moving = tick(
        moving_kernel,
        tcp_base=(0.0148, 0.0, 0.0),
        progress_overrides={"center_frozen": False},
    ).predicted_radial_bound_m
    frozen = tick(
        frozen_kernel,
        tcp_base=(0.0148, 0.0, 0.0),
        progress_overrides={"center_frozen": True},
    ).predicted_radial_bound_m
    assert frozen == moving
