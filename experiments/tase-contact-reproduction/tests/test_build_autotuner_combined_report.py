from __future__ import annotations

import json
from pathlib import Path

from build_autotuner_combined_report import build_report


def test_combined_report_emits_real_image_assets_and_mac_zip(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "step6.autotune/autotuner-v4-v5-combined-input-v1",
                "version": 1,
                "source_sha256": "a" * 64,
                "v4_stages": [
                    {"stage": "A", "mae_n": [0.8, 0.7, 0.6]},
                    {"stage": "B1", "mae_n": [0.9, 0.75, 0.65]},
                ],
                "timing_gap_ms": [3.0, 4.0, 5.0],
                "figure8_trajectory": {
                    "actual_x": [0.0, 0.1, 0.0],
                    "actual_y": [0.0, 0.0, 0.1],
                    "commanded_x": [0.0, 0.1, 0.0],
                    "commanded_y": [0.0, 0.0, 0.1],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report"
    manifest = build_report(snapshot, output, zip_path=tmp_path / "mac-report.zip")
    html = (output / "report.html").read_text(encoding="utf-8")
    assert manifest["image_count"] == 3
    assert html.count("<img ") == 3
    assert len(list((output / "images").glob("*.png"))) == 3
    assert (tmp_path / "mac-report.zip").is_file()
