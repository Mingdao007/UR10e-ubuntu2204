from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_v4_isaturation_plan_isolated_and_zero_correction() -> None:
    from step6_figure8_autotune_v1.v4_isaturation_plan import (
        V4ISaturationPlanError,
        load_v4_isaturation_plan,
    )

    raw = json.loads(
        (ROOT / "config/step6/v4_isaturation_validation_plan_v1.json").read_text()
    )
    plan = load_v4_isaturation_plan(raw)
    assert plan.candidate_n == 5
    assert plan.primary_floor_campaign_n == 200
    assert plan.as_dict()["automatic_promotion"] is False
    with pytest.raises(V4ISaturationPlanError):
        load_v4_isaturation_plan({**raw, "correction_weights": [0.1] + [0.0] * 5})
