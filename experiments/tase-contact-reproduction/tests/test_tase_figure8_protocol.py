from __future__ import annotations

import math
from pathlib import Path

import pytest

from tase_figure8_protocol import (
    DURATION_S,
    FORMAL_END_S,
    FORMAL_START_S,
    PROTOCOL_ID,
    RATE400_PROTOCOL_ID,
    REQUIRED_BINS,
    Figure8Window60Task,
    TaseFigure8ProtocolError,
    protocol_manifest,
    score_formal_samples,
)
from contact_yield_live_path import parse_live_duration
from step5d_autotune_v4_r004.timing import TimingEvidence, TimingError


def test_60s_protocol_is_explicit_and_not_full_period() -> None:
    task = Figure8Window60Task()
    assert task.protocol_id == PROTOCOL_ID
    assert task.duration_s == DURATION_S == 60.0
    assert task.as_dict()["full_period_protocol"] is False
    assert task.as_dict()["historical_compatibility"] == "R013"
    assert task.reference(0.0)["position_m"] == (0.0, 0.0, 0.0)
    assert task.reference(60.0)["protocol_id"] == PROTOCOL_ID
    assert math.isclose(task.reference(60.0)["position_m"][0], 0.04 * math.sin(6.0))
    assert protocol_manifest()["identity_sha256"] == task.identity_sha256


def test_reference_rejects_full_period_clock_and_requires_separate_identity() -> None:
    task = Figure8Window60Task()
    with pytest.raises(TaseFigure8ProtocolError):
        task.reference(60.001)
    with pytest.raises(TaseFigure8ProtocolError):
        Figure8Window60Task(protocol_id="full_period")
    request = parse_live_duration("r013_60")
    assert request.kind == "r013_compat_60"
    assert request.path_duration_s == 60.0
    assert request.protocol_id == PROTOCOL_ID


def test_rate400_is_a_separate_identity_without_reclassifying_460_hz_failure() -> None:
    request = parse_live_duration("r013_60_rate400")
    assert request.kind == "r013_compat_60"
    assert request.protocol_id == RATE400_PROTOCOL_ID
    assert Figure8Window60Task(protocol_id=RATE400_PROTOCOL_ID).as_dict()[
        "minimum_data_rate_hz"
    ] == 400.0
    counts = dict(
        duration_s=60.0,
        successful_writer_publishes=26760,
        distinct_rtde_frames=26760,
        distinct_kunwei_frames=60000,
        distinct_tp_consumed_packet_echoes=26760,
        feedback_age_p99_s=0.009,
        max_fresh_gap_s=0.008,
    )
    assert not TimingEvidence(**counts).successful
    assert TimingEvidence(
        **counts, minimum_rate_hz=400.0,
        acceptance_protocol_id=RATE400_PROTOCOL_ID,
    ).successful
    with pytest.raises(TimingError):
        TimingEvidence(**counts, minimum_rate_hz=400.0)


def test_figure8_entry_defaults_to_r013_60() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "figure8.sh").read_text()
    assert "duration='r013_60'" in script
    assert "Use --duration full explicitly" in script


def test_incomplete_and_complete_metrics_are_both_reported() -> None:
    incomplete = [
        {"path_time_s": 5.0, "filtered_normal_n": 4.0},
        {"path_time_s": 5.1, "filtered_normal_n": 6.0},
        {"path_time_s": 20.0, "filtered_normal_n": 5.0},
    ]
    partial = score_formal_samples(incomplete, stage="PATH")
    assert partial["complete"] is False
    assert partial["sample_count"] == 3
    assert partial["mae_n"] == pytest.approx(2.0 / 3.0)
    assert partial["complete_bins"] == 3

    complete = [
        {"path_time_s": FORMAL_START_S + (index + 0.01) * 0.1, "normal_force_n": 5.2}
        for index in range(REQUIRED_BINS)
    ]
    scored = score_formal_samples(complete, stage="PATH")
    assert scored["complete"] is True
    assert scored["complete_bins"] == REQUIRED_BINS
    assert scored["mae_n"] == pytest.approx(0.2)


def test_qualification_metrics_are_not_relabelled_as_formal_completion() -> None:
    scored = score_formal_samples(
        [{"path_time_s": 5.0, "normal_load_n": 4.5}], stage="QUALIFICATION"
    )
    assert scored["stage"] == "QUALIFICATION"
    assert scored["complete"] is False
    assert scored["mae_n"] == pytest.approx(0.5)
