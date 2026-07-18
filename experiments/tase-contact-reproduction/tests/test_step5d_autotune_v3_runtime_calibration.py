from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3 import runtime_calibration as calibration  # noqa: E402


def test_runtime_calibration_vector_mutation_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "runtime_calibration.json"
    shutil.copyfile(calibration.DEFAULT_ARTIFACT, target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["runtime_value"]["tcp_offset_tool0_mean_xyz_m"][2] = 0.1
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(calibration.RuntimeCalibrationError, match="norm differs"):
        calibration.load_runtime_calibration(target)


def test_runtime_calibration_is_compact_not_a_hidden_raw_trace() -> None:
    assert calibration.DEFAULT_ARTIFACT.stat().st_size < 8 * 1024
    payload = json.loads(calibration.DEFAULT_ARTIFACT.read_text(encoding="utf-8"))
    assert payload["source_evidence"]["bridge_csv_origin"].endswith(
        "bridge_rtde_500hz.csv"
    )
    assert "rows" not in payload
