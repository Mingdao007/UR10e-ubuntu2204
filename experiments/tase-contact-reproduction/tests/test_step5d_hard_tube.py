from __future__ import annotations

import math
import re
from pathlib import Path

from ur10e_experiment_runtime.hard_tube import (
    HARD_TUBE_EVALUATION_FLOOR_HZ,
    HARD_TUBE_EVALUATION_HZ,
    HARD_TUBE_PROGRESS_FRESHNESS_FLOOR_HZ,
    HARD_TUBE_PROGRESS_MAX_AGE_NS,
    HARD_TUBE_PROGRESS_STALE_DWELL_NS,
    HARD_TUBE_PROGRESS_STALE_RECOVERY_DWELL_NS,
    HARD_TUBE_RADIUS_M,
    HardTubeGuard,
    HardTubeReason,
)
from ur10e_experiment_runtime.stage_adapters import (
    KNOWN_INACTIVE_CONTROLLER_STAGES,
    KNOWN_TP_PROTOCOL_STATES,
    ControllerProgress,
    ControllerProgressPhase,
    Stage25ControllerProgressAdapter,
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
    assert HARD_TUBE_EVALUATION_FLOOR_HZ == 50.0
    assert guard.progress_max_age_ns == HARD_TUBE_PROGRESS_MAX_AGE_NS == 20_000_000
    assert guard.progress_stale_dwell_ns == HARD_TUBE_PROGRESS_STALE_DWELL_NS
    assert (
        guard.progress_stale_recovery_dwell_ns
        == HARD_TUBE_PROGRESS_STALE_RECOVERY_DWELL_NS
        == 100_000_000
    )
    assert (
        guard.progress_freshness_floor_hz
        == HARD_TUBE_PROGRESS_FRESHNESS_FLOOR_HZ
        == 50.0
    )

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


def test_full_home_stage_is_known_inactive_for_stage25_tube() -> None:
    adapter = Stage25ControllerProgressAdapter(
        physical_prior_sha256="b" * 64,
        allow_tick_gaps=True,
    )
    controller_progress = adapter.sample(
        stage=40.3,
        controller_progress_s=0.0,
        controller_tick_seq=1,
        controller_timestamp_s=1.0,
        age_ns=0,
        tcp_z_m=None,
    )
    assert controller_progress.phase is ControllerProgressPhase.INACTIVE
    result = HardTubeGuard(
        reference_sha256=adapter.reference_sha256
    ).tick(
        progress=controller_progress,
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
            progress(1, age_ns=-1),
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


def test_hard_tube_tolerates_observed_sub_50_hz_budget_rtde_jitter() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    result = guard.tick(
        progress=progress(1, age_ns=2_175_578),
        tcp_base=(0.01, 0.0, 0.0),
    )
    assert result.stop is False
    assert result.reason is HardTubeReason.TUBE_OK


def test_hard_tube_stale_age_requires_continuous_100ms_dwell() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    first = guard.tick(
        progress=progress(10, age_ns=20_000_001),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_000_000_000,
    )
    assert first.stop is False
    assert first.reason is HardTubeReason.TUBE_PROGRESS_STALE_PENDING
    spike_recovered = guard.tick(
        progress=progress(11, age_ns=0),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_010_000_000,
    )
    assert spike_recovered.stop is False
    recovery_completed = guard.tick(
        progress=progress(12, age_ns=0),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_110_000_000,
    )
    assert recovery_completed.stop is False

    pending = guard.tick(
        progress=progress(13, age_ns=20_000_001),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_120_000_000,
    )
    assert pending.stop is False
    assert pending.reason is HardTubeReason.TUBE_PROGRESS_STALE_PENDING
    still_pending = guard.tick(
        progress=progress(14, age_ns=20_000_001),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_219_999_999,
    )
    assert still_pending.stop is False
    stopped = guard.tick(
        progress=progress(15, age_ns=20_000_001),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_220_000_000,
    )
    assert stopped.stop is True
    assert stopped.reason is HardTubeReason.TUBE_PROGRESS_STALE


def test_hard_tube_stale_flapping_cannot_reset_stop_budget() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    first = guard.tick(
        progress=progress(10, age_ns=20_000_001),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_000_000_000,
    )
    assert first.reason is HardTubeReason.TUBE_PROGRESS_STALE_PENDING
    fresh_pulse = guard.tick(
        progress=progress(11, age_ns=0),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_098_000_000,
    )
    assert fresh_pulse.stop is False
    pending = guard.tick(
        progress=progress(12, age_ns=20_000_001),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_099_999_999,
    )
    assert pending.reason is HardTubeReason.TUBE_PROGRESS_STALE_PENDING
    stopped = guard.tick(
        progress=progress(13, age_ns=20_000_001),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_100_000_000,
    )
    assert stopped.stop is True
    assert stopped.reason is HardTubeReason.TUBE_PROGRESS_STALE


def test_hard_tube_stale_dwell_never_masks_geometric_breach() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    breached = guard.tick(
        progress=progress(10, age_ns=20_000_001),
        tcp_base=(0.030_001, 0.0, 0.0),
        observed_monotonic_ns=1_000_000_000,
    )
    assert breached.stop is True
    assert breached.reason is HardTubeReason.TUBE_ACTUAL_BREACH
    assert math.isclose(breached.actual_distance_m, 0.030_001, abs_tol=1e-12)


def test_hard_tube_rejects_host_monotonic_clock_regression() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    assert guard.tick(
        progress=progress(10),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=2_000_000_000,
    ).stop is False
    regressed = guard.tick(
        progress=progress(11),
        tcp_base=(0.01, 0.0, 0.0),
        observed_monotonic_ns=1_999_999_999,
    )
    assert regressed.stop is True
    assert regressed.reason is HardTubeReason.TUBE_PROGRESS_NONSEQUENTIAL


def test_hard_tube_250hz_rows_evaluate_above_50hz_floor() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    evaluated_at: list[int] = []
    base_ns = 2_000_000_000
    for index in range(26):
        observed_ns = base_ns + index * 4_000_000
        result = guard.tick(
            progress=progress(100 + index),
            tcp_base=(0.01, 0.0, 0.0),
            observed_monotonic_ns=observed_ns,
        )
        if result.reason is HardTubeReason.TUBE_OK:
            evaluated_at.append(observed_ns)
    gaps_ns = [
        later - earlier for earlier, later in zip(evaluated_at, evaluated_at[1:])
    ]
    assert len(evaluated_at) >= 9
    assert max(gaps_ns) == 12_000_000
    assert 1_000_000_000.0 / max(gaps_ns) >= HARD_TUBE_EVALUATION_FLOOR_HZ


def test_hard_tube_accepts_tick_gap_and_repeat_but_rejects_regression() -> None:
    guard = HardTubeGuard(reference_sha256=REFERENCE)
    assert guard.tick(
        progress=progress(10),
        tcp_base=(0.0, 0.0, 0.0),
    ).stop is False
    gap = guard.tick(progress=progress(12), tcp_base=(0.0, 0.0, 0.0))
    assert gap.stop is False
    assert gap.reason is HardTubeReason.TUBE_BETWEEN_SAMPLES
    repeated = guard.tick(progress=progress(12), tcp_base=(0.0, 0.0, 0.0))
    assert repeated.stop is False
    assert repeated.reason is HardTubeReason.TUBE_BETWEEN_SAMPLES
    regressed = guard.tick(
        progress=progress(11, monotonic=False),
        tcp_base=(0.0, 0.0, 0.0),
    )
    assert regressed.stop is True
    assert regressed.reason is HardTubeReason.TUBE_PROGRESS_NONSEQUENTIAL

    guard.reset()
    restarted = guard.tick(progress=progress(100), tcp_base=(0.0, 0.0, 0.0))
    assert restarted.stop is False
    assert restarted.reason is HardTubeReason.TUBE_OK


def test_tp_protocol_state_gates_uninitialized_stage_without_weakening_run() -> None:
    adapter = Stage25ControllerProgressAdapter(
        physical_prior_sha256="b" * 64,
        allow_tick_gaps=True,
    )

    bootstrap = adapter.sample(
        stage=0.0,
        controller_progress_s=0.0,
        controller_tick_seq=1,
        controller_timestamp_s=1.0,
        age_ns=0,
        tcp_z_m=0.0,
        tp_protocol_state=0,
    )
    assert bootstrap.phase is ControllerProgressPhase.INACTIVE
    ready = adapter.sample(
        stage=0.0,
        controller_progress_s=0.0,
        controller_tick_seq=2,
        controller_timestamp_s=1.002,
        age_ns=0,
        tcp_z_m=0.0,
        tp_protocol_state=10,
    )
    assert ready.phase is ControllerProgressPhase.INACTIVE
    run_unknown = adapter.sample(
        stage=0.0,
        controller_progress_s=0.0,
        controller_tick_seq=3,
        controller_timestamp_s=1.004,
        age_ns=0,
        tcp_z_m=0.0,
        tp_protocol_state=20,
    )
    assert run_unknown.phase is ControllerProgressPhase.UNKNOWN
    run_active = adapter.sample(
        stage=25.0,
        controller_progress_s=0.0,
        controller_tick_seq=4,
        controller_timestamp_s=1.006,
        age_ns=0,
        tcp_z_m=0.0,
        tp_protocol_state=20,
    )
    assert run_active.phase is ControllerProgressPhase.ACTIVE_STAGE25
    returned = adapter.sample(
        stage=0.0,
        controller_progress_s=0.0,
        controller_tick_seq=5,
        controller_timestamp_s=1.008,
        age_ns=0,
        tcp_z_m=0.0,
        tp_protocol_state=78,
    )
    assert returned.phase is ControllerProgressPhase.INACTIVE


def test_r026_stage_writes_are_covered_by_tube_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (
        root
        / "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r026.script"
    ).read_text(encoding="utf-8")
    arguments = re.findall(
        r"write_output_float_register\(35,\s*([A-Za-z_][A-Za-z0-9_]*|[0-9.]+)\)",
        script,
    )
    literal_stages = {float(value) for value in arguments if value[0].isdigit()}
    assert literal_stages == {
        20.0,
        22.0,
        23.0,
        25.05,
        25.15,
        25.3,
        25.95,
        25.0,
        29.0,
        40.3,
    }
    assert literal_stages - {25.0} <= set(KNOWN_INACTIVE_CONTROLLER_STAGES)
    assert {"stage_code", "search_stage", "near_stage"} <= set(arguments)
    wait_stage_calls = {
        float(value)
        for value in re.findall(
            r"(?m)^\s*stop_reason\s*=\s*codex_wait_for_cmd_valid\(([0-9.]+),",
            script,
        )
    }
    search_stage_calls = {
        (float(far), float(near))
        for far, near in re.findall(
            r"(?m)^\s*stop_reason\s*=\s*codex_step5d_down_search"
            r"\(([0-9.]+),\s*([0-9.]+),",
            script,
        )
    }
    assert wait_stage_calls == {25.05}
    assert search_stage_calls == {(24.0, 24.2)}
    assert script.count("codex_wait_for_stage_linear_zero(") == 1
    parameterized_stages = wait_stage_calls | {
        stage for pair in search_stage_calls for stage in pair
    }
    assert parameterized_stages <= set(KNOWN_INACTIVE_CONTROLLER_STAGES)


def test_r026_protocol_state_writes_are_covered_by_tube_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (
        root
        / "programs/step5/step5d/step5d_strict_rnn_autotune_v3_r026.script"
    ).read_text(encoding="utf-8")
    explicit_states: set[int] = set()
    for function_name in (
        "codex_autotune_write_state",
        "codex_autotune_wait_for_arm",
        "codex_autotune_wait_for_external_home",
        "codex_autotune_publish_state_and_halt",
    ):
        explicit_states.update(
            int(value)
            for value in re.findall(
                rf"{function_name}\([^,\n]+,[^,\n]+,\s*([0-9]+),",
                script,
            )
        )
    assert explicit_states == {10, 11, 20, 30, 40, 50, 60, 75, 77, 78, 90}
    assert explicit_states <= KNOWN_TP_PROTOCOL_STATES
