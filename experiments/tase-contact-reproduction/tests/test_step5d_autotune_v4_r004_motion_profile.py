"""Deterministic offline tests for the r004 V3-to-V4 motion adapter."""

from __future__ import annotations

import ast
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SOURCE))
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r004.motion_profile import (  # noqa: E402
    R004_EXECUTION_PROFILE,
    R004_MOTION_PROFILE,
    SameDirectionQdotRescale,
    same_direction_qdot_rescale,
)
from step5d_autotune_v4_r004.runtime import (  # noqa: E402
    gate_qdot,
    rescale_qdot_to_gate,
)


class FakeContract:
    model_hashes = {}


def identity_jacobian() -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(1.0 if row == column else 0.0 for column in range(6))
        for row in range(6)
    )


def test_r004_profile_binds_exact_frozen_v3_values() -> None:
    profile = R004_MOTION_PROFILE
    assert profile.v3_control_profile_id == "step5d_strict_rnn_autotune_v1"
    assert profile.execution_profile is R004_EXECUTION_PROFILE
    assert profile.profile_id == "nf100000-slew250-a2000"
    assert profile.xy_path_speed_m_s == 0.004
    assert profile.total_linear_cap_m_s == 1.0
    assert profile.normal_linear_cap_m_s == 1.0
    assert profile.angular_cap_rad_s == 0.25
    assert profile.qdot_cap_rad_s == 2.5
    assert profile.host_slew_rad_s2 == 2.5
    assert profile.tp_acceleration_rad_s2 == 20.0
    assert profile.normal_update_rate_rad_s == 100.0
    assert profile.cadence_hz == 500.0
    assert profile.period_s == 0.002


def test_profile_and_rescale_result_are_immutable_and_typed() -> None:
    with pytest.raises((FrozenInstanceError, AttributeError)):
        R004_MOTION_PROFILE.qdot_cap_rad_s = 0.1  # type: ignore[misc]
    result = same_direction_qdot_rescale(
        (0.5, 0.1, 0.0, 0.0, 0.0, 0.0),
        dt_s=0.002,
        max_slew_rad_s2=R004_MOTION_PROFILE.host_slew_rad_s2,
    )
    assert isinstance(result, SameDirectionQdotRescale)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        result.scale = 1.0  # type: ignore[misc]


def test_same_direction_rescale_preserves_delta_direction_not_components() -> None:
    result = same_direction_qdot_rescale(
        (0.5, 0.1, 0.0, 0.0, 0.0, 0.0),
        dt_s=0.002,
        max_slew_rad_s2=2.5,
    )
    assert result.delta_limit_rad_s == 0.005
    assert result.qdot[:2] == pytest.approx((0.005, 0.001))
    assert result.qdot[1] / result.qdot[0] == pytest.approx(0.2)
    assert result.qdot[1] != pytest.approx(0.005)


def test_same_direction_rescale_clamps_dt_to_v3_maximum() -> None:
    result = same_direction_qdot_rescale(
        (0.5, 0.1, 0.0, 0.0, 0.0, 0.0),
        dt_s=0.100,
        max_slew_rad_s2=R004_MOTION_PROFILE.host_slew_rad_s2,
        dt_max_s=0.020,
    )
    assert result.delta_limit_rad_s == 0.05
    assert result.qdot[:2] == pytest.approx((0.05, 0.01))


def test_profile_gate_recomputes_jacobian_and_rejects_profile_caps() -> None:
    jacobian = identity_jacobian()
    contract = FakeContract()
    allowed = gate_qdot(
        contract,
        qdot=(0.0, 0.0, 0.5, 0.0, 0.0, 0.0),
        jacobian_6x6=jacobian,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes={},
        motion_profile=R004_MOTION_PROFILE,
    )
    assert allowed.allowed
    assert allowed.twist == pytest.approx((0.0, 0.0, 0.5, 0.0, 0.0, 0.0))
    assert allowed.normal_m_s == pytest.approx(0.5)

    angular = gate_qdot(
        contract,
        qdot=(0.0, 0.0, 0.0, 0.0, 0.0, 0.3),
        jacobian_6x6=jacobian,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes={},
        motion_profile=R004_MOTION_PROFILE,
    )
    assert not angular.allowed
    assert angular.reason == "angular_cap"
    assert angular.qdot == (0.0,) * 6

    xy = gate_qdot(
        contract,
        qdot=(0.005, 0.0, 0.0, 0.0, 0.0, 0.0),
        jacobian_6x6=jacobian,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes={},
        motion_profile=R004_MOTION_PROFILE,
    )
    assert not xy.allowed
    assert xy.reason == "tangential_component_cap"


def test_default_gate_keeps_the_other_v4_revision_envelope() -> None:
    result = gate_qdot(
        FakeContract(),
        qdot=(0.0, 0.0, 0.0, 0.0, 0.0, 0.1),
        jacobian_6x6=identity_jacobian(),
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes={},
    )
    assert not result.allowed
    assert result.reason == "angular_cap"


def test_rescale_to_gate_has_v3_scalar_and_profile_gate_semantics() -> None:
    result, scale = rescale_qdot_to_gate(
        FakeContract(),
        qdot=(0.5, 0.1, 0.0, 0.0, 0.0, 0.0),
        previous_qdot=(0.0,) * 6,
        jacobian_6x6=identity_jacobian(),
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes={},
        actual_dt_s=0.002,
        motion_profile=R004_MOTION_PROFILE,
    )
    assert scale == pytest.approx(0.01)
    assert not result.allowed
    assert result.reason == "tangential_component_cap"
    assert result.qdot == (0.0,) * 6
    assert result.twist[:2] == pytest.approx((0.005, 0.001))


def test_runtime_and_qualification_expose_profile_injection_without_live_setup() -> None:
    runtime_source = (
        ROOT / "tools/step5d_autotune_v4_r004/calibrated_runtime.py"
    ).read_text()
    runtime_tree = ast.parse(runtime_source)
    runtime_init = next(
        node
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assert any(argument.arg == "motion_profile" for argument in runtime_init.args.kwonlyargs)

    qualification_source = (
        ROOT / "tools/step5d_autotune_v4_r004/qualification.py"
    ).read_text()
    qualification_tree = ast.parse(qualification_source)
    qualification_class = next(
        node
        for node in qualification_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CanonicalQualificationControl"
    )
    profile_field = next(
        node
        for node in qualification_class.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "motion_profile"
    )
    assert isinstance(profile_field.value, ast.Name)
    assert profile_field.value.id == "R004_MOTION_PROFILE"
