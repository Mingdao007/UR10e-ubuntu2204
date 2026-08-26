from __future__ import annotations

from pathlib import Path
import math
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r013.contact_search_strategy import (  # noqa: E402
    A0_HARD_TWO_STAGE,
    A1_BOUNDED_TANH_SIGMOID,
    ContactSearchStrategyError,
    a0_strategy,
    a1_strategy,
    schedule_rows,
)
from step5d_autotune_v4_r013.controller_triplet import (  # noqa: E402
    R013_A1_PROGRAM,
    build_a1_sigmoid_canary_triplet,
    transform_a1_sigmoid_controller_script,
    validate_a1_sigmoid_script,
)
from step5d_autotune_v4_r013.campaign import FIXED_KI_SEEDS, candidate_token  # noqa: E402
from step5d_autotune_v4_r013.identity import CampaignFingerprint  # noqa: E402
from step5d_autotune_v4_r013.live_owner import (  # noqa: E402
    R013OwnerError,
    _fresh_trial_flow_gates,
    _strict_physical_admission_from_record,
)


def test_a0_and_a1_share_endpoints_but_a1_has_no_velocity_step() -> None:
    a0 = a0_strategy()
    a1 = a1_strategy()
    boundary = a0.boundary_m

    assert a0.strategy_id == A0_HARD_TWO_STAGE
    assert a1.strategy_id == A1_BOUNDED_TANH_SIGMOID
    assert a1.speed_m_s(0.0) == pytest.approx(a1.far_speed_m_s)
    assert a1.speed_m_s(boundary) == pytest.approx(a1.near_speed_m_s)
    assert a1.speed_m_s(boundary - 0.005) == pytest.approx(
        (a1.far_speed_m_s + a1.near_speed_m_s) / 2.0, abs=2e-5
    )
    assert a1.speed_m_s(boundary - 1e-7) < a0.speed_m_s(boundary - 1e-7)

    values = [a1.speed_m_s(boundary * index / 20.0) for index in range(21)]
    assert all(values[index] >= values[index + 1] for index in range(20))
    assert all(a1.near_speed_m_s <= value <= a1.far_speed_m_s for value in values)


def test_schedule_rows_are_explicitly_command_contract_rows() -> None:
    rows = schedule_rows(points=11)
    assert len(rows) == 22
    assert {row["series"] for row in rows} == {
        A0_HARD_TWO_STAGE,
        A1_BOUNDED_TANH_SIGMOID,
    }
    assert all("boundary_mm" in row and "transition_width_mm" in row for row in rows)


def test_strategy_rejects_invalid_bounds() -> None:
    with pytest.raises(ContactSearchStrategyError):
        a1_strategy().__class__(strategy_id=A1_BOUNDED_TANH_SIGMOID, transition_width_m=0.0)
    with pytest.raises(ContactSearchStrategyError):
        a1_strategy().__class__(strategy_id=A1_BOUNDED_TANH_SIGMOID, far_speed_m_s=0.0001)


def test_a1_controller_transform_is_isolated_and_validated(tmp_path: Path) -> None:
    source_path = ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r012.script"
    script = transform_a1_sigmoid_controller_script(
        source_path.read_text(encoding="utf-8"),
        stamp="2026-08-15T0000Z_STEP5D_AUTOTUNE_V4_R013_A1_613014",
    )
    checks = validate_a1_sigmoid_script(script)
    assert all(checks.values())
    assert R013_A1_PROGRAM in script
    assert "step5d_strict_rnn_autotune_v4_r013\n" not in script
    assert "pow(2.718281828, z)" in script
    assert "local z = 12.000000000 * (2.0 * u - 1.0)" in script

    paths = build_a1_sigmoid_canary_triplet(
        tmp_path,
        source_script_path=source_path,
        stamp="2026-08-15T0000Z_STEP5D_AUTOTUNE_V4_R013_A1_613014",
    )
    assert {path.name for path in paths.values()} == {
        f"{R013_A1_PROGRAM}.script",
        f"{R013_A1_PROGRAM}.txt",
        f"{R013_A1_PROGRAM}.urp",
        f"{R013_A1_PROGRAM}.numeric-sanity.json",
    }


def test_a1_urscript_logistic_is_numerically_equivalent_to_python_tanh6() -> None:
    """The resident formula must implement the exact canonical tanh schedule."""

    strategy = a1_strategy()
    edge = math.exp(12.0)
    sigmoid_lo = 1.0 / (1.0 + edge)
    sigmoid_hi = edge / (1.0 + edge)
    for index in range(101):
        u = index / 100.0
        z = 12.0 * (2.0 * u - 1.0)
        sigmoid = math.exp(z) / (1.0 + math.exp(z))
        bounded = (sigmoid - sigmoid_lo) / (sigmoid_hi - sigmoid_lo)
        travel = strategy.boundary_m - strategy.transition_width_m + u * strategy.transition_width_m
        expected = strategy.far_speed_m_s + (
            strategy.near_speed_m_s - strategy.far_speed_m_s
        ) * bounded
        assert strategy.speed_m_s(travel) == pytest.approx(expected, abs=3e-12)


def test_ordinary_trial_admission_requires_complete_lifecycle_receipt() -> None:
    result = SimpleNamespace(
        safe_return=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        identity_gate=True,
        duration_s=60.0,
        metrics={
            "r013_force_lifecycle_complete": False,
            "force_objective": {"complete_bins": 550, "required_bins": 550},
        },
    )
    with pytest.raises(R013OwnerError, match="complete force lifecycle"):
        _fresh_trial_flow_gates(result)
    result.metrics["r013_force_lifecycle_complete"] = True
    _fresh_trial_flow_gates(result)

    candidate = {
        "force_p_gain": 0.006727171322157698,
        "force_damping": 56.0,
        "force_i_gain": FIXED_KI_SEEDS[0],
        "i_off": False,
        "normal_filter_tau_s": 0.05202781128136904,
        "orientation_ko": 0.05946035575013606,
        "motion_kp": 1.5,
        "target_force_n": 5.0,
    }
    dispatch = SimpleNamespace(
        dispatch_id="r013-physical-admission",
        candidate=candidate,
        candidate_token=candidate_token(candidate),
    )
    record = SimpleNamespace(
        candidate=SimpleNamespace(canonical=candidate),
        attempt_sequence=4,
        execution_id="physical-execution",
        mae_n=0.8,
        eligible=True,
        timing_gate=True,
        motion_gate=True,
        qualification_passed=False,
        observation_uid="physical-observation",
        sealed=True,
        metrics={"execution_id": "physical-execution"},
    )
    fingerprint = CampaignFingerprint(
        path_id="r013-test-path",
        metric_fingerprint="force-mae-v2-sealed",
        handoff_policy="freeze_carry_v1",
        correction_runtime_strategy_identity="disabled",
        source_identity="fresh",
        eoat_identity="eoat-v4",
        home_tare_identity="home-tare-v1",
        controller_lineage="r013-lineage",
    )
    qualification_records = tuple(
        SimpleNamespace(qualification_eligible=True) for _ in range(3)
    )
    admission = _strict_physical_admission_from_record(
        dispatch=dispatch,
        record=record,
        qualification_records=qualification_records,
        campaign_fingerprint=fingerprint,
    )
    assert admission.epoch_qualification_passed is True
    assert admission.trial_admission_passed is True
    assert admission.qualification_passed is False
