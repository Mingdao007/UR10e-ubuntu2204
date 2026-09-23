"""Ten no-disturbance image pairs gate later workpiece claims."""
import cv2
import numpy as np
import pytest

from admit_tase_board_corridor import INPUT_SCHEMA, admit_corridor


def _manifest(tmp_path, *, out_of_corridor_pair=None):
    rng = np.random.default_rng(5)
    before = np.clip(170 + rng.normal(0, 6, (240, 320)), 0, 255).astype("uint8")
    cv2.rectangle(before, (12, 12), (307, 227), 205, 2)
    cv2.line(before, (20, 46), (290, 46), 130, 2)
    corridor = np.zeros_like(before)
    path = np.array([[30, 126], [80, 117], [130, 126],
                     [180, 117], [230, 126], [280, 117]])
    cv2.polylines(corridor, [path], False, 255, 25)
    cv2.polylines(before, [path], False, 90, 3)
    mask_path = tmp_path / "corridor.png"
    assert cv2.imwrite(str(mask_path), corridor)
    pairs = []
    for index in range(10):
        after = before.copy()
        cv2.polylines(after, [path], False, 80 - index, 5)
        if index == out_of_corridor_pair:
            cv2.line(after, (115, 72), (175, 68), 36, 4)
        before_path = tmp_path / f"before-{index}.png"
        after_path = tmp_path / f"after-{index}.png"
        assert cv2.imwrite(str(before_path), before)
        assert cv2.imwrite(str(after_path), after)
        pairs.append({"run_id": f"normal-{index}",
                      "before_captured_at_s": 1000 + index * 100,
                      "after_captured_at_s": 1080 + index * 100,
                      "before": str(before_path), "after": str(after_path)})
    return {"schema": INPUT_SCHEMA, "corridor_mask": str(mask_path),
            "unperturbed_capture_pairs": pairs}


def test_ten_independent_normal_traces_admit_only_the_declared_corridor(tmp_path):
    receipt = admit_corridor(_manifest(tmp_path))
    assert receipt["unperturbed_runs"] == 10
    assert receipt["detectable"] is True
    assert receipt["frozen_before_candidate"] is True
    assert len(receipt["mask_sha256"]) == 64
    assert receipt["max_registration_error_px"] < 1.5
    assert all(row["measurement"]["outside_corridor_area_px2"] == 0
               for row in receipt["observations"])


def test_unintended_normal_mark_and_duplicate_capture_withhold_baseline(tmp_path):
    manifest = _manifest(tmp_path, out_of_corridor_pair=7)
    receipt = admit_corridor(manifest)
    assert receipt["detectable"] is False
    assert receipt["frozen_before_candidate"] is False
    assert "pair_7:normal_trace_outside_declared_corridor" in receipt["failure_reasons"]
    manifest["unperturbed_capture_pairs"][9]["after"] = manifest["unperturbed_capture_pairs"][8]["after"]
    with pytest.raises(ValueError, match="independent regular files"):
        admit_corridor(manifest)


def test_duplicate_capture_content_or_run_identity_is_not_ten_independent_runs(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["unperturbed_capture_pairs"][9]["run_id"] = "normal-8"
    with pytest.raises(ValueError, match="run IDs must be unique"):
        admit_corridor(manifest)
    manifest["unperturbed_capture_pairs"][9]["run_id"] = "normal-9"
    # Distinct filenames and timestamps still cannot turn a copied frame pair
    # into a second physical baseline attempt.
    duplicate_after = tmp_path / "copied-after.png"
    duplicate_after.write_bytes((tmp_path / "after-8.png").read_bytes())
    manifest["unperturbed_capture_pairs"][9]["after"] = str(duplicate_after)
    with pytest.raises(ValueError, match="repeat identical image content"):
        admit_corridor(manifest)
