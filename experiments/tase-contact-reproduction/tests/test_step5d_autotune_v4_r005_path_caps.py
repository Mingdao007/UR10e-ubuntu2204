"""Focused offline proof for the r004 typed PATH motion-cap binding."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SOURCE))
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4.contracts import V4Candidate  # noqa: E402
from step5d_autotune_v4_r004.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r004.motion_profile import (  # noqa: E402
    R004_MOTION_PROFILE,
)
from step5d_autotune_v4_r004.path_controller import V4PathController  # noqa: E402
from step5d_autotune_v4_r004.policies import V4InvariantEnvelope  # noqa: E402
from step5d_autotune_v4_r004.runtime import gate_qdot as r004_gate_qdot  # noqa: E402
from step5d_autotune_v4_r004.wire import _finite_qdot  # noqa: E402
from step5d_autotune_v4_r004.path_reference import step5_path_reference  # noqa: E402


class FakeContract:
    model_hashes = {}


def identity_jacobian() -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(1.0 if row == column else 0.0 for column in range(6))
        for row in range(6)
    )


def test_r004_production_path_owners_resolve_typed_v3_caps() -> None:
    contract = load_contract()
    runtime = contract.motion_runtime
    profile = R004_MOTION_PROFILE

    assert runtime.schema == "step5d.autotune-v4/r004-motion-profile-v1"
    assert runtime.version == "v3-r034-effective-caps-v1"
    assert runtime.source == "frozen_v3_r034"
    assert runtime.bridge_motion_limit_m_s == pytest.approx(0.004)
    assert (
        runtime.path_xy_speed_m_s,
        runtime.normal_linear_speed_m_s,
        runtime.total_linear_speed_m_s,
        runtime.angular_speed_rad_s,
        runtime.qdot_abs_rad_s,
        runtime.host_qdot_slew_rad_s2,
        runtime.tp_speedj_acceleration_rad_s2,
    ) == pytest.approx((0.004, 1.0, 1.0, 0.25, 2.5, 2.5, 20.0))
    assert (
        profile.xy_path_speed_m_s,
        profile.normal_linear_cap_m_s,
        profile.total_linear_cap_m_s,
        profile.angular_cap_rad_s,
        profile.qdot_cap_rad_s,
        profile.host_slew_rad_s2,
        profile.tp_acceleration_rad_s2,
    ) == pytest.approx((0.004, 1.0, 1.0, 0.25, 2.5, 2.5, 20.0))

    controller = V4PathController(
        V4Candidate(),
        motion_profile=profile,
    )
    # Greybox velocity damping: v_ss = P·e/D.  Use a huge force error so
    # steady-state exceeds the typed normal cap and the clamp is exercised.
    tick = None
    for _ in range(400):
        tick = controller.step(
            actual_dt_s=profile.period_s,
            raw_normal_n=0.0,
            setpoint_n=1_000_000.0,
            mode="path",
            tangential_error_m=(100.0, -100.0),
            orientation_error_rad=(100.0, -100.0, 100.0),
        )
        if abs(tick.proposed_qdot[2]) > 0.9:
            break
    assert tick is not None
    assert abs(tick.proposed_qdot[0]) <= profile.tangential_cap_m_s + 1e-12
    assert abs(tick.proposed_qdot[1]) <= profile.tangential_cap_m_s + 1e-12
    assert abs(tick.proposed_qdot[0]) == pytest.approx(0.004, abs=1e-5)
    assert abs(tick.proposed_qdot[1]) == pytest.approx(0.004, abs=1e-5)
    assert 0.9 < abs(tick.proposed_qdot[2]) <= profile.normal_linear_cap_m_s
    assert all(
        abs(value) <= profile.angular_cap_rad_s
        for value in tick.proposed_qdot[3:]
    )

    identity = identity_jacobian()
    r004_allowed = r004_gate_qdot(
        FakeContract(),
        qdot=(0.0, 0.0, 0.5, 0.0, 0.0, 0.0),
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes={},
        motion_profile=profile,
    )
    assert r004_allowed.allowed
    assert not r004_gate_qdot(
        FakeContract(),
        qdot=(0.005, 0.0, 0.0, 0.0, 0.0, 0.0),
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes={},
        motion_profile=profile,
    ).allowed
    assert not r004_gate_qdot(
        FakeContract(),
        qdot=(0.0, 0.0, 0.0, 0.0, 0.0, 0.3),
        jacobian_6x6=identity,
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes={},
        motion_profile=profile,
    ).allowed

    envelope = V4InvariantEnvelope(motion_profile=profile)
    assert (
        envelope.cartesian_total_cap_m_s,
        envelope.normal_cap_m_s,
        envelope.tangential_cap_m_s,
        envelope.angular_cap_rad_s,
        envelope.qdot_cap_rad_s,
    ) == pytest.approx((1.0, 1.0, 0.004, 0.25, 2.5))
    assert _finite_qdot((2.5, 0.0, 0.0, 0.0, 0.0, 0.0), motion_profile=profile)[0] == pytest.approx(2.5)

    qualification_source = (
        ROOT / "tools/step5d_autotune_v4_r004/qualification.py"
    ).read_text(encoding="utf-8")
    assert "from step5d_autotune_v4_r004.path_controller import V4PathController" in qualification_source
    assert "from step5d_autotune_v4_r004.policies import V4InvariantEnvelope" in qualification_source
    assert "from step5d_autotune_v4.path_controller import" not in qualification_source
    assert "from step5d_autotune_v4.policies import" not in qualification_source
    assert "V4PathController(\n                canonical_candidate,\n                motion_profile=self.motion_profile," in qualification_source
    assert "V4InvariantEnvelope(\n                motion_profile=self.motion_profile," in qualification_source

    calibrated_tree = ast.parse(
        (ROOT / "tools/step5d_autotune_v4_r004/calibrated_runtime.py").read_text(
            encoding="utf-8"
        )
    )
    init = next(
        node
        for node in ast.walk(calibrated_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assert any(argument.arg == "motion_profile" for argument in init.args.kwonlyargs)
    calibrated_source = (
        ROOT / "tools/step5d_autotune_v4_r004/calibrated_runtime.py"
    ).read_text(encoding="utf-8")
    assert "from step5d_autotune_v4_r004.path_controller import derive_force_terms" in calibrated_source


def test_frozen_r002_r003_shared_bindings_are_byte_stable() -> None:
    expected_shared = {
        "tools/step5d_autotune_v4/policies.py":
            "9d8ec13dc38fb84e2510f0fab5d63934a22c7f70f1d7192743567dadcdaadd75",
        "tools/step5d_autotune_v4/calibrated_runtime.py":
            "811cbdd1a75786ef9dbc071b53179419bc0a839782d83d0358a5a1c4b7bfd4dd",
    }
    for revision in ("r002", "r003"):
        document = json.loads(
            (ROOT / f"config/step5d/autotune_v4_{revision}.json").read_text(
                encoding="utf-8"
            )
        )
        for binding_name in ("policy_binding", "robot_model_binding"):
            binding = document.get(binding_name, {})
            for key, relative in binding.items():
                if not key.endswith("_path") or relative not in expected_shared:
                    continue
                hash_key = key.removesuffix("_path") + "_sha256"
                declared = binding[hash_key]
                actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                assert declared == expected_shared[relative]
                assert actual == declared


def test_v1_cycloid_60s_is_not_structurally_impossible_under_path_caps() -> None:
    contract = load_contract()
    duration_s = float(contract.raw["runtime"]["path_runtime_s"])
    assert duration_s == 60.0

    speeds: list[float] = []
    for index in range(6001):
        now_s = duration_s * index / 6000.0
        reference = step5_path_reference(
            "step5d_strict_rnn_autotune_v1",
            (0.0, 0.0),
            now_s,
        )
        velocity = tuple(float(value) for value in reference["desired_velocity_xy"])
        assert all(math.isfinite(value) for value in velocity)
        speeds.append(math.hypot(*velocity))

    maximum_speed = max(speeds)
    assert maximum_speed > 0.0
    assert maximum_speed == pytest.approx(0.003, abs=1e-9)
    assert maximum_speed < R004_MOTION_PROFILE.xy_path_speed_m_s
    assert maximum_speed <= R004_MOTION_PROFILE.total_linear_cap_m_s
