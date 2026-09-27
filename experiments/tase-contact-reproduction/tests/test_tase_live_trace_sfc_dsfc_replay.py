import math
import sys
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from replay_tase_live_trace_sfc_dsfc import command_row_to_replay_row  # noqa: E402


def _source_row():
    return {
        "schema": "tase.qp-replay-command-timeline-v3",
        "reference_phase": "path",
        "reference_time_s": 5.0,
        "host_monotonic_s": 123.4,
        "actual_dt_s": 0.002,
        "force_target_n": 5.0,
        "published_packet_qdot_rad_s": [0.01] * 6,
        "actual_q_rad": [0.1] * 6,
        "solver_qdot_lower_rad_s": [-0.15] * 6,
        "solver_qdot_upper_rad_s": [0.15] * 6,
        "jacobian_6x6": np.eye(6).tolist(),
        "sfc_replay_inputs": {
            "control_wrench_tcp_n_nm": [1.0, 2.0, 3.0, 0.1, 0.2, 0.3],
            "native_wrench_tcp_n_nm": [4.0, 5.0, 6.0, 0.4, 0.5, 0.6],
            "actual_tcp_pose_m_rad": [0.4, 0.5, 0.6, 0.0, 0.0, math.pi / 2],
            "actual_tcp_speed_m_s_rad_s": [0.01, 0.02, 0.03, 0.04, 0.05, 0.06],
            "control_reaction_normal_base": [0.0, 0.0, 1.0],
            "reference_position_base_m": [0.45, 0.55, 0.6],
            "reference_velocity_base_m_s": [0.0, 0.01, 0.0],
            "state_age_s": 0.004,
        },
        "sfc_replay_input_provenance": {
            "control_wrench_tcp_n_nm": "kunwei_baseline_corrected",
            "native_wrench_tcp_n_nm": "kunwei_native",
            "control_reaction_normal_base": "fixed_base_z_control_axis",
        },
    }


def test_live_trace_converter_maps_measured_inputs_without_reconstruction():
    row = command_row_to_replay_row(_source_row())
    inputs = row["inputs"]
    assert row["time_s"] == 123.4
    assert inputs["path_reference_time_s"] == 5.0
    assert row["dt_s"] == 0.002
    np.testing.assert_allclose(inputs["tcp_position_base_m"], [0.4, 0.5, 0.6])
    np.testing.assert_allclose(inputs["actual_tcp_speed_m_s_rad_s"], [0.01, 0.02, 0.03, 0.04, 0.05, 0.06])
    np.testing.assert_allclose(inputs["raw_force_base_n"], [-2.0, 1.0, 3.0], atol=1e-12)
    np.testing.assert_allclose(inputs["force_control_torque_base_nm"], [-0.2, 0.1, 0.3], atol=1e-12)
    np.testing.assert_allclose(inputs["native_force_base_n"], [-5.0, 4.0, 6.0], atol=1e-12)
    np.testing.assert_allclose(inputs["estimated_outward_normal_base"], [0.0, 0.0, 1.0])
    np.testing.assert_allclose(row["output"]["realized_jqdot_m_s_rad_s"], [0.01] * 6)
    assert "independent task-force" in inputs["force_signal_provenance"]
    assert "fixed approved base-Z" in inputs["normal_signal_provenance"]


@pytest.mark.parametrize("field", ["reference_position_base_m", "state_age_s"])
def test_live_trace_converter_fails_closed_on_missing_required_input(field):
    row = _source_row()
    row["sfc_replay_inputs"].pop(field)
    with pytest.raises((TypeError, ValueError)):
        command_row_to_replay_row(row)


def test_live_trace_converter_rejects_wrong_schema_or_nonpath_rows():
    row = _source_row()
    row["schema"] = "tase.qp-replay-command-timeline-v2"
    with pytest.raises(ValueError, match="v3 formal PATH"):
        command_row_to_replay_row(row)
