from __future__ import annotations

from ur10e_experiment_runtime.moving_sphere import (
    MovingSphereKernel,
    SphereReason,
    StoppingBoundArtifact,
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


def tick(kernel: MovingSphereKernel, **overrides):
    values = dict(
        stage=25.0,
        tcp_base=(0.0, 0.0, 0.0),
        center_base=(0.0, 0.0, 0.0),
        tcp_speed_m_s=0.0,
        progress_age_ns=2_000_000,
        progress_reference_sha256=REF,
        phase_frozen=False,
    )
    values.update(overrides)
    return kernel.tick(**values)


def test_actual_and_predicted_breaches_have_distinct_reasons() -> None:
    kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=certified())
    assert tick(kernel, tcp_base=(0.016, 0.0, 0.0)).reason is SphereReason.SPHERE_ACTUAL_BREACH
    result = tick(kernel, tcp_base=(0.014, 0.0, 0.0), tcp_speed_m_s=0.1)
    assert result.reason is SphereReason.SPHERE_PREDICTED_STOP_BREACH


def test_missing_nonfinite_reference_stale_and_uncertified_fail_closed() -> None:
    kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=certified())
    assert tick(kernel, tcp_base=None).reason is SphereReason.SPHERE_INPUT_MISSING
    assert tick(kernel, tcp_speed_m_s=float("nan")).reason is SphereReason.SPHERE_INPUT_NONFINITE
    assert tick(kernel, progress_reference_sha256="c" * 64).reason is SphereReason.SPHERE_REFERENCE_MISMATCH
    assert tick(kernel, progress_age_ns=2_000_001).reason is SphereReason.SPHERE_PROGRESS_STALE
    kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=None)
    assert tick(kernel).reason is SphereReason.SPHERE_STOP_BOUND_UNCERTIFIED


def test_inactive_stage_does_not_enforce_and_frozen_center_reduces_bound() -> None:
    kernel = MovingSphereKernel(reference_sha256=REF, stopping_bound=certified())
    assert not tick(kernel, stage=24.0, tcp_base=None).stop
    moving = tick(kernel, tcp_base=(0.0148, 0.0, 0.0), phase_frozen=False).predicted_radial_bound_m
    frozen = tick(kernel, tcp_base=(0.0148, 0.0, 0.0), phase_frozen=True).predicted_radial_bound_m
    assert frozen < moving
