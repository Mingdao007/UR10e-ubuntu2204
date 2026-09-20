"""Return-decision regression for the rotation-vector branch seen on the bench.

Only the mature execute/return decision is exercised with endpoint doubles;
the fixture deliberately excludes admission and never qualifies hardware.
"""
import math

import pytest

from step5d_autotune_v4_r004_live_writer import LiveWriterError
from test_contact_yield_writer_loop import exercise_writer_loop


def test_mature_return_accepts_same_attitude_across_pi_branch(tmp_path, monkeypatch):
    evidence, _, _ = exercise_writer_loop(
        tmp_path, monkeypatch,
        home_rotation=(math.pi, 0., 0.),
        terminal_rotation=(-math.pi, 0., 0.),
        native_provider=True,
    )
    assert evidence.return_gate_passed


def test_mature_return_rejects_actual_attitude_error_above_original_limit(tmp_path, monkeypatch):
    with pytest.raises(LiveWriterError, match="return_home_guard_failed"):
        exercise_writer_loop(
            tmp_path, monkeypatch,
            home_rotation=(math.pi, 0., 0.),
            terminal_rotation=(math.pi - .02, 0., 0.),
            native_provider=True,
        )
