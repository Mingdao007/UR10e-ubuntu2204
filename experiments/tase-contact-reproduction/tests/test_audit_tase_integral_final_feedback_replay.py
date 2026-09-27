from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from audit_tase_integral_final_feedback_replay import (  # noqa: E402
    _downstream_freeze_sign,
)


def _row(**updates):
    row = {
        "published_feedback_present": True,
        "published_task_space_saturated": True,
        "published_realization_saturated": False,
        "host_slew_limited": False,
        "published_task_normal_m_s": 0.003,
        "published_realized_normal_m_s": 0.003,
        "force_error_n": 1.0,
    }
    row.update(updates)
    return row


def test_task_space_limit_alone_does_not_trigger_downstream_freeze():
    assert _downstream_freeze_sign(_row()) == 0


def test_final_realization_residual_freezes_only_when_it_worsens_error():
    assert _downstream_freeze_sign(
        _row(published_realization_saturated=True, published_realized_normal_m_s=0.002)
    ) == 1
    assert _downstream_freeze_sign(
        _row(
            published_realization_saturated=True,
            published_realized_normal_m_s=0.002,
            force_error_n=-1.0,
        )
    ) == -1


def test_host_slew_requires_a_directional_normal_residual():
    assert _downstream_freeze_sign(_row(host_slew_limited=True)) == 0
    assert _downstream_freeze_sign(
        _row(host_slew_limited=True, published_realized_normal_m_s=0.002)
    ) == 1


def test_missing_successful_publication_feedback_fails_closed():
    with pytest.raises(ValueError, match="incomplete"):
        _downstream_freeze_sign(
            _row(published_feedback_present=True, published_realization_saturated=True,
                 published_realized_normal_m_s=None)
        )
