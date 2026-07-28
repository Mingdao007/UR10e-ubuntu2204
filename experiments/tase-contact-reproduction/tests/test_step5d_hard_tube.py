from __future__ import annotations

import math

from ur10e_experiment_runtime.hard_tube import (
    HARD_TUBE_EVALUATION_HZ,
    HARD_TUBE_RADIUS_M,
    HardTubeGuard,
    HardTubeReason,
)
from ur10e_experiment_runtime.stage_adapters import (
    ControllerProgress,
    ControllerProgressPhase,
)


REFERENCE = "a" * 64


def progress(
    sequence: int,
    *,
    phase: ControllerProgressPhase = ControllerProgressPhase.ACTIVE_STAGE25,
    age_ns: int = 0,
    reference: str = REFERENCE,
    monotonic: bool = True,
) -> ControllerProgress:
    return ControllerProgress(
        phase=phase,
        controller_tick_seq=sequence,
        controller_timestamp_ns=sequence * 2_000_000,
        age_ns=age_ns,
        progress_s=0.0,
        center_x_m=0.0,
        center_y_m=0.0,
        center_z_m=0.0,
        reference_sha256=reference,
        monotonic=monotonic,
    )


def test_hard_tube_is_30_mm_and_evaluates_at_100_hz() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    assert guard.radius_m == HARD_TUBE_RADIUS_M == 0.030
    assert guard.evaluation_hz == HARD_TUBE_EVALUATION_HZ == 100.0

    first = guard.tick(progress=progress(100), tcp_base=(0.029, 0.0, 0.0))
    assert first.stop is False
    assert first.reason is HardTubeReason.TUBE_OK
    assert math.isclose(first.remaining_margin_m, 0.001)

    for sequence in range(101, 105):
        held = guard.tick(
            progress=progress(sequence),
            tcp_base=(0.031, 0.0, 0.0),
        )
        assert held.stop is False
        assert held.reason is HardTubeReason.TUBE_BETWEEN_SAMPLES
        assert math.isclose(held.actual_distance_m, 0.029)

    breach = guard.tick(progress=progress(105), tcp_base=(0.031, 0.0, 0.0))
    assert breach.stop is True
    assert breach.reason is HardTubeReason.TUBE_ACTUAL_BREACH
    assert math.isclose(breach.actual_distance_m, 0.031)


def test_hard_tube_known_inactive_stage_allows_without_geometry() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    result = guard.tick(
        progress=progress(1, phase=ControllerProgressPhase.INACTIVE),
        tcp_base=None,
    )
    assert result.stop is False
    assert result.reason is HardTubeReason.TUBE_INACTIVE


def test_hard_tube_fail_closed_input_contracts() -> None:
    cases = (
        (None, (0.0, 0.0, 0.0), HardTubeReason.TUBE_INPUT_MISSING),
        (
            progress(1, phase=ControllerProgressPhase.UNKNOWN),
            (0.0, 0.0, 0.0),
            HardTubeReason.TUBE_STAGE_UNKNOWN,
        ),
        (
            progress(1, reference="b" * 64),
            (0.0, 0.0, 0.0),
            HardTubeReason.TUBE_REFERENCE_MISMATCH,
        ),
        (
            progress(1, age_ns=2_000_001),
            (0.0, 0.0, 0.0),
            HardTubeReason.TUBE_PROGRESS_STALE,
        ),
        (
            progress(1, monotonic=False),
            (0.0, 0.0, 0.0),
            HardTubeReason.TUBE_PROGRESS_NONSEQUENTIAL,
        ),
        (
            progress(1),
            (math.nan, 0.0, 0.0),
            HardTubeReason.TUBE_INPUT_NONFINITE,
        ),
    )
    for controller_progress, tcp, expected in cases:
        result = HardTubeGuard(reference_sha256=REFERENCE).tick(
            progress=controller_progress,
            tcp_base=tcp,
        )
        assert result.stop is True
        assert result.reason is expected


def test_hard_tube_accepts_tick_gap_but_rejects_repeat_and_reset_restarts() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    assert guard.tick(
        progress=progress(10),
        tcp_base=(0.0, 0.0, 0.0),
    ).stop is False
    gap = guard.tick(progress=progress(12), tcp_base=(0.0, 0.0, 0.0))
    assert gap.stop is False
    assert gap.reason is HardTubeReason.TUBE_BETWEEN_SAMPLES
    repeated = guard.tick(progress=progress(12), tcp_base=(0.0, 0.0, 0.0))
    assert repeated.stop is True
    assert repeated.reason is HardTubeReason.TUBE_PROGRESS_NONSEQUENTIAL

    guard.reset()
    restarted = guard.tick(progress=progress(100), tcp_base=(0.0, 0.0, 0.0))
    assert restarted.stop is False
    assert restarted.reason is HardTubeReason.TUBE_OK
