from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import measure_contact_board_marks as board_marks  # noqa: E402


def _synthetic_board() -> tuple[np.ndarray, np.ndarray]:
    height, width = 240, 320
    rng = np.random.default_rng(14)
    image = np.clip(174 + rng.normal(0, 6, (height, width)), 0, 255).astype(np.uint8)
    cv2.rectangle(image, (12, 12), (307, 227), 206, 2)
    cv2.line(image, (24, 46), (292, 46), 138, 2)
    cv2.circle(image, (44, 190), 13, 112, 2)

    corridor = np.zeros((height, width), dtype=np.uint8)
    path_points = np.array([[38, 126], [82, 117], [128, 126], [178, 116], [224, 126], [280, 117]])
    cv2.polylines(corridor, [path_points], False, 255, thickness=25)
    # An existing normal 5 N trace lies inside the declared corridor.
    cv2.polylines(image, [path_points], False, 91, thickness=3)
    return image, corridor


def _shift(image: np.ndarray, x: float, y: float) -> np.ndarray:
    matrix = np.array([[1.0, 0.0, x], [0.0, 1.0, y]], dtype=np.float32)
    return cv2.warpAffine(image, matrix, (image.shape[1], image.shape[0]), borderValue=174)


def test_reports_external_new_mark_and_permits_normal_5n_path_marks() -> None:
    before, corridor = _synthetic_board()
    after = before.copy()
    cv2.line(after, (118, 76), (174, 69), 38, thickness=4)
    # Simulate small fixed-camera drift; registration should recover the board.
    after = _shift(after, 2.0, -1.0)

    result = board_marks.measure_contact_board_marks(
        before,
        after,
        corridor,
        pixel_size_mm=0.25,
    )

    assert result["schema_version"] == "contact-board-marks.v1"
    assert result["qualified"] is True
    assert result["finding"] == "new_visible_change_outside_corridor"
    assert result["expected_5n_path_marks_permitted"] is True
    assert result["outside_corridor_area_px2"] > 0
    assert result["max_outside_distance_px"] > 0
    assert result["outside_corridor_area_mm2"] == result["outside_corridor_area_px2"] * 0.25**2
    assert result["registration_error_px"] <= 1.5
    assert result["detectability"]["repeatability"]["qualified"] is True
    assert result["detectability"]["capture_repeatability_measured"] is False
    # Results are directly consumable as strict JSON and contain no NaN/Inf.
    assert json.loads(json.dumps(result, allow_nan=False))["qualified"] is True


def test_path_only_trace_is_permitted_and_physical_scale_is_omitted() -> None:
    before, corridor = _synthetic_board()
    after = before.copy()
    path_points = np.array(
        [[38, 126], [82, 117], [128, 126], [178, 116], [224, 126], [280, 117]]
    )
    cv2.polylines(after, [path_points], False, 72, thickness=5)

    result = board_marks.measure_contact_board_marks(before, after, corridor)

    assert result["qualified"] is True
    assert result["finding"] == "no_detected_change_outside_corridor"
    assert result["outside_corridor_area_px2"] == 0
    assert result["max_outside_distance_px"] is None
    assert result["outside_corridor_area_mm2"] is None
    assert result["max_outside_distance_mm"] is None


def test_saturation_failure_is_fail_closed() -> None:
    before, corridor = _synthetic_board()
    after = before.copy()
    # Clipping most off-path board pixels removes usable surface detail.
    after[:] = 255
    after[~(corridor > 0)] = 255

    result = board_marks.measure_contact_board_marks(before, after, corridor)

    assert result["qualified"] is False
    assert result["finding"] == "unqualified"
    assert result["outside_corridor_area_px2"] is None
    assert "no damage" not in result["claim_boundary"].lower()
    assert any("saturation" in reason for reason in result["failure_reasons"])


def test_mismatched_shapes_return_explicit_unqualified_json() -> None:
    before, corridor = _synthetic_board()
    after = before[:-1, :]

    result = board_marks.measure_contact_board_marks(before, after, corridor)

    assert result["qualified"] is False
    assert result["outside_corridor_area_px2"] is None
    assert "dimensions differ" in result["failure_reasons"][0]


def test_threshold_sensitive_mark_fails_repeatability_gate() -> None:
    before, corridor = _synthetic_board()
    after = before.copy()
    # This deliberately weak, tiny mark appears at the base threshold but
    # disappears under the +15 percent sensitivity check.
    cv2.line(after, (120, 70), (126, 70), 155, thickness=1)

    result = board_marks.measure_contact_board_marks(before, after, corridor)

    assert result["qualified"] is False
    assert result["outside_corridor_area_px2"] is None
    assert result["detectability"]["repeatability"]["qualified"] is False
    assert result["detectability"]["repeatability"]["areas_px2"][-1] == 0
    assert "repeatability" in result["failure_reasons"][-1]


def test_cli_writes_and_prints_json_result(tmp_path: Path, capsys) -> None:
    before, corridor = _synthetic_board()
    before_path = tmp_path / "before.png"
    after_path = tmp_path / "after.png"
    mask_path = tmp_path / "corridor.png"
    output_path = tmp_path / "result.json"
    assert cv2.imwrite(str(before_path), before)
    assert cv2.imwrite(str(after_path), before)
    assert cv2.imwrite(str(mask_path), corridor)

    exit_code = board_marks.main(
        [
            "--before",
            str(before_path),
            "--after",
            str(after_path),
            "--corridor-mask",
            str(mask_path),
            "--output",
            str(output_path),
        ]
    )

    stdout_result = json.loads(capsys.readouterr().out)
    file_result = json.loads(output_path.read_text())
    assert exit_code == 0
    assert stdout_result == file_result
    assert stdout_result["schema_version"] == "contact-board-marks.v1"
    expected_mask_sha256 = hashlib.sha256(mask_path.read_bytes()).hexdigest()
    assert stdout_result["corridor_mask_sha256"] == expected_mask_sha256
    assert stdout_result["corridor_mask_sha256_basis"] == "raw_file_bytes"
