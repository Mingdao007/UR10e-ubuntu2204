from __future__ import annotations

import math

from step5d_autotune_v4_r008.host_hard_tube import (
    PATH_END_OVERRUN_ALLOWANCE_S,
    HostHardTubeGuard,
)
from ur10e_experiment_runtime.stage_adapters import (
    PATH_ORIGIN_XY_M,
    frozen_step5d_path_reference,
)


def _pose(*, x: float = PATH_ORIGIN_XY_M[0], y: float = PATH_ORIGIN_XY_M[1], z: float = 0.02):
    return (x, y, z, 0.0, 0.0, 0.0)


def _endpoint_pose() -> tuple[float, float, float, float, float, float]:
    reference = frozen_step5d_path_reference(PATH_ORIGIN_XY_M, 60.0)
    desired = reference["desired_xy"]
    return _pose(x=float(desired[0]), y=float(desired[1]))


def test_endpoint_projection_keeps_hard_tube_finite_and_active() -> None:
    guard = HostHardTubeGuard.from_environ(force_enabled=True)
    for index, path_time_s in enumerate((59.999, 60.0, 60.005)):
        decision = guard.evaluate(
            tp_state=25,
            path_time_s=path_time_s,
            tcp_pose_m_rad=_endpoint_pose(),
            monotonic_ns=1_000_000_000 + index * 20_000_000,
            controller_timestamp_s=1.0 + index * 0.02,
        )
        assert decision.stop is False
        assert decision.reason in {"TUBE_OK", "TUBE_BETWEEN_SAMPLES", "PATH_END_OVERRUN_PENDING"}
        assert decision.actual_distance_m is not None
        assert math.isfinite(decision.actual_distance_m)


def test_endpoint_overrun_beyond_allowance_stops_explicitly() -> None:
    guard = HostHardTubeGuard.from_environ(force_enabled=True)
    decision = guard.evaluate(
        tp_state=25,
        path_time_s=60.0 + PATH_END_OVERRUN_ALLOWANCE_S + 0.001,
        tcp_pose_m_rad=_endpoint_pose(),
        monotonic_ns=1_000_000_000,
        controller_timestamp_s=1.0,
    )
    assert decision.stop is True
    assert decision.reason == "PATH_END_OVERRUN"


def test_endpoint_projection_does_not_bypass_actual_breach() -> None:
    guard = HostHardTubeGuard.from_environ(force_enabled=True)
    decision = guard.evaluate(
        tp_state=25,
        path_time_s=60.005,
        tcp_pose_m_rad=_pose(x=_endpoint_pose()[0] + 0.10, y=_endpoint_pose()[1]),
        monotonic_ns=1_000_000_000,
        controller_timestamp_s=1.0,
    )
    assert decision.stop is True
    assert decision.reason == "TUBE_ACTUAL_BREACH"


def test_real_nonfinite_pose_remains_fail_closed() -> None:
    guard = HostHardTubeGuard.from_environ(force_enabled=True)
    decision = guard.evaluate(
        tp_state=25,
        path_time_s=59.9,
        tcp_pose_m_rad=_pose(x=float("nan")),
        monotonic_ns=1_000_000_000,
        controller_timestamp_s=1.0,
    )
    assert decision.stop is True
    assert decision.reason == "TUBE_INPUT_NONFINITE"
