from __future__ import annotations

import math

import pytest

from step5d_tacdiffusion_direct_torque import (
    FIXED_DAMPING,
    FIXED_STIFFNESS,
    FixtureShadowRunner,
    JOINT_DAMPING,
    RuntimeSample,
    SoftwareWrenchBaseline,
    Step5dDirectTorqueCore,
)


def sample(tick: int, *, wrench=(0.0,) * 6, qd=(0.0,) * 6, tau=(0.0,) * 6):
    return RuntimeSample(
        tick=tick,
        elapsed_s=tick * 0.002,
        tcp_pose_base=(0.487795411149049, 0.12932679270060748, 0.02, 3.14, 0.0, 0.0),
        tcp_speed_base=(0.0,) * 6,
        joint_position_rad=(0.6, -1.6, -2.6, -0.5, 1.5, -1.0),
        joint_speed_rad_s=qd,
        joint_torque_nm=tau,
        wrench_tcp_si=wrench,
        control_reaction_normal_base=(0.0, 0.0, -1.0),
    )


def run(evaluator):
    core = Step5dDirectTorqueCore(
        lease_id=77,
        shadow=FixtureShadowRunner(evaluator),
    )
    return [core.tick(sample(tick)) for tick in range(25)]


def test_fixed_direct_torque_contract_is_exact() -> None:
    assert FIXED_STIFFNESS == (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
    assert FIXED_DAMPING == (
        69.2820323,
        69.2820323,
        69.2820323,
        4.898979486,
        4.898979486,
        4.898979486,
    )
    assert JOINT_DAMPING == (1.5, 1.5, 1.2, 0.3, 0.3, 0.2)


def test_fixture_shadow_is_50hz_and_command_bytes_are_bitwise_invariant() -> None:
    cases = {
        "off": None,
        "finite": lambda item: (float(item.tick), 1.0, 2.0, 3.0, 4.0, 5.0),
        "crash": lambda item: (_ for _ in ()).throw(RuntimeError("fixture crash")),
        "nan": lambda item: (math.nan, 1.0, 2.0, 3.0, 4.0, 5.0),
        "overflow": lambda item: (math.inf, 1.0, 2.0, 3.0, 4.0, 5.0),
    }
    runs = {name: run(evaluator) for name, evaluator in cases.items()}
    baseline = [result.command_bytes for result in runs["off"]]
    for name, results in runs.items():
        assert [result.command_bytes for result in results] == baseline, name
        assert all(result.packet.model_mode == 1 for result in results)
        assert all(result.packet.raw_feedforward_wrench == (0.0,) * 6 for result in results)
    assert [runs["finite"][tick].shadow.sequence for tick in (0, 9, 10, 19, 20)] == [1, 1, 2, 2, 3]
    assert runs["crash"][0].shadow.status == "crashed"
    assert runs["nan"][0].shadow.status == "invalid"
    assert runs["overflow"][0].shadow.status == "invalid"


def test_exact_1000_sample_software_baseline() -> None:
    baseline = SoftwareWrenchBaseline()
    for _ in range(999):
        baseline.add((1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
    assert not baseline.ready
    with pytest.raises(RuntimeError, match="incomplete"):
        baseline.bias()
    baseline.add((1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
    assert baseline.ready
    assert baseline.bias() == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    with pytest.raises(RuntimeError, match="already complete"):
        baseline.add((0.0,) * 6)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    (
        ({"wrench": (51.0, 0.0, 0.0, 0.0, 0.0, 0.0)}, "force_norm_guard"),
        ({"wrench": (0.0, 0.0, 0.0, 3.1, 0.0, 0.0)}, "torque_norm_guard"),
        ({"qd": (1.01, 0.0, 0.0, 0.0, 0.0, 0.0)}, "joint_speed_guard"),
        ({"tau": (20.1, 0.0, 0.0, 0.0, 0.0, 0.0)}, "joint_torque_guard"),
    ),
)
def test_runtime_guards_fail_closed(kwargs, reason) -> None:
    core = Step5dDirectTorqueCore(lease_id=1, shadow=FixtureShadowRunner(None))
    with pytest.raises(RuntimeError, match=reason):
        core.tick(sample(0, **kwargs))


def test_equilibrium_slew_is_bounded_and_packets_are_contiguous() -> None:
    results = run(None)
    for previous, current in zip(results, results[1:]):
        translation = math.dist(previous.equilibrium_pose[:3], current.equilibrium_pose[:3])
        orientation = math.dist(previous.equilibrium_pose[3:], current.equilibrium_pose[3:])
        assert translation <= 0.05 * 0.002 + 1e-12
        assert orientation <= 0.05 * 0.002 + 1e-12
        assert current.packet.sequence_after == previous.packet.sequence_after + 1

