"""Focused acceptance tests for the bounded common-registry characterization."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_contact_qp import build  # noqa: E402
from contact_yield_kinematics import load_kinematics  # noqa: E402
from characterize_tase_registry import characterize  # noqa: E402


@pytest.fixture(scope="module")
def qp_library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("tase-characterization-qp"))


@pytest.fixture(scope="module")
def ur10e_kinematics():
    return load_kinematics(require_ur10e=True)


def test_aligned_nominal_is_shared_across_methods_and_dt_half(qp_library: Path, ur10e_kinematics) -> None:
    result = characterize(
        qp_library=qp_library,
        duration_s=0.02,
        dt_s=0.002,
        include_half_dt=True,
        kinematics=ur10e_kinematics,
    )
    assert result["schema"] == "tase-registry-characterization-v2"
    assert result["provenance"]["kinematics"]["kind"] == "ur10e_calibrated_pinocchio"
    assert result["equation_mapping"]["source_pdf_page"] == 6

    nominal = result["runs"]["aligned_nominal"]["dt_runs"]
    assert set(nominal) == {"dt_0.002000_s", "dt_0.001000_s"}
    for run in nominal.values():
        assert run["frozen_task_diagnostics"]["infeasible_unconstrained_steps"] == 0
        assert run["shared_input"]["twist_consistency_norm"]["max"] < 1e-12
        for name, method in run["methods"].items():
            assert method["first_failure"] is None
            assert method["trace"]["accepted_steps"] == run["steps_requested"]
            assert method["trace"]["shared_outer_task_match_max_abs"] < 1e-12
            assert method["invalid_input_handling"]["accepted"] is False
            assert method["invalid_input_handling"]["state_unchanged"] is True
            assert method["metadata"]["offline_only"] is True
            assert method["metadata"]["live_eligible"] is False
        assert run["methods"]["TASE_RNN"]["metadata"]["lambda_update_sign"] == "plus"
        assert run["methods"]["TASE_RNN_MATURE_MINUS"]["metadata"]["lambda_update_sign"] == "minus"


def test_orientation_stress_preserves_qp_failure_boundary(qp_library: Path, ur10e_kinematics) -> None:
    result = characterize(
        qp_library=qp_library,
        duration_s=0.002,
        dt_s=0.002,
        include_half_dt=True,
        kinematics=ur10e_kinematics,
    )
    stress = result["runs"]["orientation_task_stress"]["dt_runs"]
    for run in stress.values():
        task = run["frozen_task_diagnostics"]
        assert task["first_step"]["desired_current_orientation_angle_rad"] > 0.5
        assert task["first_step"]["unconstrained_within_bounds"] is False
        assert task["first_step"]["unconstrained_bound_violation"] > 1.0
        qp = run["methods"]["TASE_QP"]
        assert qp["trace"]["accepted_steps"] == 0
        assert qp["first_failure"]["step_index"] == 0
        assert qp["first_failure"]["type"] == "QpError"
        assert qp["first_failure"]["state_unchanged_after_registry_rollback"] is True
        assert "status=3" in qp["first_failure"]["message"]
        assert run["methods"]["TASE_RNN"]["first_failure"] is None
        assert run["methods"]["TASE_RNN_MATURE_MINUS"]["first_failure"] is None
