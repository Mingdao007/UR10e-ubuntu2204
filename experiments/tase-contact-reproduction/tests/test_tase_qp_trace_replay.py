import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contact_qp import QpError, QpResult  # noqa: E402
import replay_tase_qp_trace as replay  # noqa: E402


def _row():
    return {
        "packet_sequence": 11,
        "reference_time_s": 0.0,
        "actual_dt_s": 0.002,
        "solver_qdot_lower_rad_s": [-0.1] * 6,
        "solver_qdot_upper_rad_s": [0.1] * 6,
        "slew_adjusted_qdot_lower_rad_s": [-0.01] * 6,
        "slew_adjusted_qdot_upper_rad_s": [0.01] * 6,
        "jacobian_6x6": [[1.0 if i == j else 0.0 for j in range(6)] for i in range(6)],
        "requested_outer_twist_m_s_rad_s": [0.0] * 6,
        "previous_published_qdot_rad_s": [0.0] * 6,
        "host_slew_delta_limit_rad_s": 0.01,
        "published_packet_qdot_rad_s": [0.0] * 6,
    }


def test_captured_slew_reuses_the_recorded_scalar_delta_limit():
    row = _row()
    adjusted, scale = replay._apply_captured_slew(row, [0.05, -0.025, 0, 0, 0, 0])
    assert scale == pytest.approx(0.2)
    assert adjusted == pytest.approx([0.01, -0.005, 0, 0, 0, 0])


class _FakeSolver:
    def __init__(self, _library, *, deadline_s):
        assert deadline_s is None
        self.state = 0
        self.fail = False

    def reset(self):
        self.state = 0

    def snapshot(self):
        return {"state": self.state}

    def restore(self, state):
        self.state = state["state"]

    def solve(self, _jacobian, _twist, _lower, _upper):
        if self.fail:
            raise QpError("OSQP did not solve accurately: status=3")
        self.state += 1
        return QpResult((0.0,) * 6, 2, 0.0, 0.0, 0.0, 0.0, 0.0001)


def test_replay_attempt_is_offline_and_records_qp_results(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "command_timeline.jsonl").write_text(json.dumps(_row()) + "\n")
    library = tmp_path / "libcontact_qp.so"
    library.write_bytes(b"test-library")
    monkeypatch.setattr(replay, "inspect_attempt", lambda _path: {
        "replay_status": "ready_for_offline_qp_solver_comparison"
    })
    summary = replay.replay_attempt(
        attempt, library, tmp_path / "out", solver_factory=_FakeSolver
    )
    assert summary["offline_only"] is True
    assert summary["closed_loop"] is False
    assert summary["independent_per_tick"]["solved_rows"] == 1
    assert summary["sequential_warm_state_prefix"]["accepted_prefix_rows"] == 1
    row = json.loads((tmp_path / "out/per-tick-qp-results.jsonl").read_text())
    assert row["qdot_after_captured_slew_rad_s"] == [0.0] * 6


def test_sequential_qp_rejection_keeps_failed_prefix_and_state_rollback():
    solver = _FakeSolver(None, deadline_s=None)
    solver.fail = True
    result = replay.run_sequential_prefix([_row()], solver)
    assert result["accepted_prefix_rows"] == 0
    assert result["first_failure"]["solver_state_unchanged_on_rejection"] is True
    assert result["complete_frozen_sequence"] is False
