from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from validate_robotics_integration_test_matrix import MatrixError, validate


MATRIX = ROOT / "config/robotics_integration_test_matrix_v1.json"


def test_release_matrix_covers_incident_from_small_through_target_hardware() -> None:
    report = validate(ROOT, MATRIX)
    assert report["ok"] is True
    assert report["lanes"] == ["hil_no_motion", "large_ursim", "medium", "small"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["lanes"].pop("hil_no_motion"), "exact"),
        (
            lambda value: value["lanes"]["large_ursim"].__setitem__(
                "container_image", "universalrobots/ursim_e-series:5.25.2"
            ),
            "pinned",
        ),
        (
            lambda value: value["lanes"]["hil_no_motion"].__setitem__(
                "motion_allowed", True
            ),
            "no-motion",
        ),
        (
            lambda value: value["lanes"]["large_ursim"].pop("programs_volume"),
            "persistent volume",
        ),
        (
            lambda value: value["requirements"][0].__setitem__("lanes", ["small"]),
            "cross every",
        ),
    ],
)
def test_release_matrix_rejects_missing_realism_and_safety_guards(
    tmp_path: Path, mutation, message: str
) -> None:
    payload = copy.deepcopy(json.loads(MATRIX.read_text(encoding="utf-8")))
    mutation(payload)
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MatrixError, match=message):
        validate(ROOT, path)
