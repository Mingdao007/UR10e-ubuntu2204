from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r013.timing_characterization import (  # noqa: E402
    classify_observations,
)
from step5d_autotune_v4_r013.live_runtime import (  # noqa: E402
    R013LightweightTimingEvidenceCollector,
)
from step5d_autotune_v4_r013.live_owner import (  # noqa: E402
    R013OwnerError,
    R013PathProfileV1,
    _fresh_trial_flow_gates,
)
from types import SimpleNamespace


def _row(
    *,
    ratio: float = 0.958,
    writer_hz: float = 493.0,
    rtde_hz: float = 493.0,
    tp_hz: float = 472.0,
    kunwei_hz: float = 1000.0,
    p99_s: float = 0.009,
    gap_s: float = 0.004,
) -> dict[str, object]:
    return {
        "timing": {
            "writer_hz": writer_hz,
            "rtde_hz": rtde_hz,
            "tp_hz": tp_hz,
            "kunwei_hz": kunwei_hz,
            "tp_writer_ratio": ratio,
            "feedback_p99_s": p99_s,
            "max_fresh_gap_s": gap_s,
        }
    }


def test_stable_coalescing_band_is_only_a_classification() -> None:
    result = classify_observations([_row(ratio=0.956 + i * 0.0005) for i in range(10)])
    assert result["classification"] == "coalescing_candidate"
    assert result["legacy_ratio_pass_count"] == 0
    assert result["legacy_ratio_remains_diagnostic"] is True
    assert result["automatic_gate_change"] is False
    assert result["statistics_ddof"] == 1


def test_low_tp_rate_is_a_hard_timing_failure() -> None:
    rows = [_row() for _ in range(9)] + [_row(tp_hz=455.0, ratio=0.94)]
    result = classify_observations(rows)
    assert result["classification"] == "controller_or_host_timing_failure"
    assert any("tp_hz_below_460" in row["failures"] for row in result["hard_failures"])


def test_insufficient_rows_are_not_promoted() -> None:
    result = classify_observations([_row() for _ in range(3)])
    assert result["classification"] == "inconclusive_insufficient_observations"


def test_lightweight_collector_preserves_distinct_layer_semantics() -> None:
    collector = R013LightweightTimingEvidenceCollector()
    collector.observe_layered_sample(
        0.0,
        source_sequences={"writer": 10, "rtde": 20.0, "kunwei": 100, "tp": 7},
        source_ages_s={"writer": 0.001, "rtde": 0.002, "tp": 0.003},
        epoch=1,
    )
    collector.observe_layered_sample(
        0.002,
        source_sequences={"writer": 11, "rtde": 21.0, "kunwei": 103, "tp": 9},
        source_ages_s={"writer": 0.001, "rtde": 0.002, "tp": 0.003},
        epoch=1,
    )
    collector.observe_layered_sample(
        0.004,
        source_sequences={"writer": 11, "rtde": 21.0, "kunwei": 103, "tp": 9},
        source_ages_s={"writer": 0.001, "rtde": 0.002, "tp": 0.003},
        epoch=1,
    )
    evidence = collector.finalize(duration_s=0.004)
    assert evidence.successful_writer_publishes == 2
    assert evidence.distinct_rtde_frames == 2
    assert evidence.distinct_kunwei_frames == 4
    assert evidence.distinct_tp_consumed_packet_echoes == 2
    assert evidence.feedback_age_p99_s == 0.003


def test_path_lifecycle_overrun_is_not_admitted_as_exact() -> None:
    result = SimpleNamespace(
        safe_return=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        identity_gate=True,
        duration_s=60.0,
        metrics={
            "r013_force_lifecycle_complete": True,
            "r013_force_lifecycle": {
                "path_duration_s": 61.7,
                "max_gap_s": 0.004,
                "max_rtde_gap_s": 0.004,
            },
            "force_objective": {"complete_bins": 550, "required_bins": 550},
        },
    )
    try:
        _fresh_trial_flow_gates(result, profile=R013PathProfileV1.cycloid())
    except R013OwnerError as exc:
        assert "PATH duration" in str(exc)
    else:  # pragma: no cover - the gate must reject the overrun
        raise AssertionError("PATH overrun was admitted")
