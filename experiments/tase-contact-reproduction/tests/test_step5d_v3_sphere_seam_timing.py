from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import benchmark_step5d_v3_sphere_seam as timing  # noqa: E402


def test_source_exact_sphere_seam_has_no_stop_or_compute_deadline_regression() -> None:
    report = timing.run(samples=500, paced=False)
    assert report["pass"] is True
    assert report["sphere_stop_count"] == 0
    assert report["compute"]["deadline_miss_count"] == 0
    assert report["compute"]["p99_ms"] <= 2.0
    assert report["schedule"]["paced"] is False
    assert "offline-only fixture bound" in report["claim_boundary"]
