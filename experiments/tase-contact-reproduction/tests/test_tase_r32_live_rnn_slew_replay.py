from __future__ import annotations

import pytest

from tools.replay_tase_r32_rnn_output_slew import replay_rows


def _row(*, previous, published, scale, host_limited, events=None):
    return {
        "actual_dt_s": 0.002,
        "jacobian_6x6": [[1.0 if i == j else 0.0 for j in range(6)] for i in range(6)],
        "requested_outer_twist_m_s_rad_s": [0.0] * 6,
        "solver_qdot_lower_rad_s": [-0.15] * 6,
        "solver_qdot_upper_rad_s": [0.15] * 6,
        "previous_published_qdot_rad_s": previous,
        "published_packet_qdot_rad_s": published,
        "host_slew_scale": scale,
        "host_slew_limited": host_limited,
        "transition_events": events or [],
    }


def _origin():
    return {
        "event_type": "path_origin",
        "event_identity": 0,
        "theta_dot_state": [0.01, 0, 0, 0, 0, 0],
        "lambda_state": [0.02, 0, 0, 0, 0, 0],
    }


def test_replay_uses_scale_on_delta_from_previous_command():
    # With identity J and zero target, the next solver output changes the
    # first joint state. The recorded qdot is halfway between old and new.
    previous = [0.01, 0, 0, 0, 0, 0]
    proposed = 0.01 + 0.002 / 0.022 * (0.02 - 0.01)
    published = [previous[0] + 0.5 * (proposed - previous[0]), 0, 0, 0, 0, 0]
    report = replay_rows([_row(
        previous=previous,
        published=published,
        scale=0.5,
        host_limited=True,
        events=[_origin()],
    )])

    assert report["recorded_delta_slew_vs_published"]["matched_rows"] == 1
    assert report["recorded_delta_slew_vs_published"]["max_rad_s"] < 1e-15
    assert report["pre_slew_rnn_vs_published"]["mismatch_rows"] == [0]
    assert report["incorrect_absolute_scale_vs_published"]["mismatch_rows"] == [0]


def test_replay_rejects_disagreeing_transition_snapshots():
    second = _origin()
    second["theta_dot_state"] = [0.02, 0, 0, 0, 0, 0]
    row = _row(
        previous=[0] * 6,
        published=[0] * 6,
        scale=1.0,
        host_limited=False,
        events=[_origin(), second],
    )
    with pytest.raises(ValueError, match="ambiguous transition snapshots"):
        replay_rows([row])


def test_replay_rejects_slew_flag_scale_disagreement():
    row = _row(
        previous=[0] * 6,
        published=[0] * 6,
        scale=0.5,
        host_limited=False,
        events=[_origin()],
    )
    with pytest.raises(ValueError, match="slew flag disagrees"):
        replay_rows([row])
