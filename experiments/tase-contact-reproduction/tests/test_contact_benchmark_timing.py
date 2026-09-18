from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contact_benchmark_timing import TimingConfig, run_writer_timing  # noqa: E402


def test_offline_writer_timing_exercises_entry_seam_and_formal_path() -> None:
    receipt = run_writer_timing(
        experiment_root=ROOT,
        controllers=("LAC",),
        config=TimingConfig(ticks=510),
    )["controllers_results"][0]
    assert receipt["errors"] == []
    assert receipt["phase_counts"] == {
        "baseline": 1,
        "entry": 501,
        "pause": 1,
        "path": 7,
    }
    assert receipt["state21_pause_count"] == 1
    assert receipt["seam"]["path_started"] is True
    assert receipt["seam"]["stationary_freeze_carry"] is True
    assert receipt["freshness"]["stale_stop_count"] == 0
    assert receipt["freshness"]["held_fraction"] > 0.0
    assert receipt["timing"]["deadline_reject_count"] == 0
    assert receipt["timing"]["sample_count"] == 510


def test_timing_route_rejects_rpsfc_and_invalid_ticks() -> None:
    with pytest.raises(ValueError, match="unknown"):
        run_writer_timing(
            experiment_root=ROOT,
            controllers=("RPSFC",),
            config=TimingConfig(ticks=510),
        )
    with pytest.raises(ValueError, match="positive integer"):
        TimingConfig(ticks=0)
