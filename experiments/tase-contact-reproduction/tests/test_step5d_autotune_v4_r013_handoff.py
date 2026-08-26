from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.step5d_autotune_v4_r013.handoff import (
    BLIND_RESET_V0,
    FREEZE_CARRY_V1,
    BlindResetHandoff,
    FreezeCarryHandoff,
    HandoffPolicy,
)
from tools.step5d_autotune_v4_r013.live_owner import _handoff_receipt_complete
import tools.step5d_autotune_v4_r013.live_runtime as live_runtime_module
from tools.step5d_autotune_v4_r013.live_runtime import _make_runtime_class
from tools.step5d_autotune_v4_r013.state21_baseline_trace import (
    STATE21_BASELINE_TRACE_SCHEMA,
    State21BaselineTrace,
    build_state21_row,
)
from step5d_paper_outer_loop import Step5dOuterLoopState


def _state(integral: float, normal_velocity: float) -> Step5dOuterLoopState:
    return Step5dOuterLoopState(
        force_integral_n_s=integral,
        xdot_p_prev_m_s=(0.0, 0.0, normal_velocity),
    )


def test_freeze_carry_records_zero_state_discontinuity_at_first_path_tick() -> None:
    handoff = FreezeCarryHandoff()
    baseline = _state(0.42, -0.0012)
    handoff.observe_baseline_update(_state(0.30, -0.0010), baseline)
    handoff.begin_path(baseline)
    handoff.observe_path_update(baseline, _state(0.43, -0.0011), path_time_s=0.0)

    receipt = handoff.receipt()
    assert receipt["policy"] == FREEZE_CARRY_V1
    assert receipt["path_clock_started_in_baseline"] is False
    assert receipt["baseline_observed"] is True
    assert receipt["first_path_tick_observed"] is True
    assert receipt["tangential_orientation_started_with_path"] is True
    assert receipt["continuity_ok"] is True
    assert receipt["integral_carry_delta_n_s"] == 0.0
    assert receipt["normal_velocity_carry_delta_m_s"] == 0.0


def test_freeze_carry_marks_a_blind_reset_as_incomplete() -> None:
    handoff = FreezeCarryHandoff()
    baseline = _state(0.42, -0.0012)
    handoff.observe_baseline_update(_state(0.30, -0.0010), baseline)
    handoff.begin_path(baseline)
    handoff.observe_path_update(_state(0.0, 0.0), _state(0.01, 0.0), path_time_s=0.0)

    receipt = handoff.receipt()
    assert receipt["continuity_ok"] is False
    assert receipt["integral_carry_delta_n_s"] == -0.42
    assert receipt["normal_velocity_carry_delta_m_s"] == 0.0012


@pytest.mark.parametrize(
    ("handoff", "path_input", "continuity_ok"),
    (
        (FreezeCarryHandoff(), _state(0.42, -0.0012), True),
        (BlindResetHandoff(), _state(0.0, 0.0), False),
    ),
)
def test_live_gate_accepts_each_complete_typed_handoff_limb(
    handoff: FreezeCarryHandoff,
    path_input: Step5dOuterLoopState,
    continuity_ok: bool,
) -> None:
    baseline = _state(0.42, -0.0012)
    handoff.observe_baseline_update(_state(0.30, -0.0010), baseline)
    handoff.begin_path(baseline)
    handoff.observe_path_update(path_input, path_input, path_time_s=0.0)

    receipt = handoff.receipt()
    assert receipt["status"] == "complete"
    assert receipt["continuity_ok"] is continuity_ok
    assert _handoff_receipt_complete(receipt) is True

    receipt["first_path_tick_observed"] = False
    assert _handoff_receipt_complete(receipt) is False


def test_r013_path_entry_handoff_policy_controls_outer_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Base:
        def __init__(self) -> None:
            self.candidate = SimpleNamespace()
            self._outer_state = _state(0.42, -0.0012)

        def desired_twist(self, *, mode: str, **kwargs):
            return (0.0,) * 6

    freeze_runtime = _make_runtime_class(Base)()
    freeze_runtime.desired_twist(mode="path", path_time_s=0.0, internal_setpoint_n=5.0)

    assert freeze_runtime._outer_state == _state(0.42, -0.0012)
    assert freeze_runtime._r013_handoff.path_entry_state is not None
    assert freeze_runtime.r013_reset_reasons == ("candidate_dispatch", "path_entry")

    monkeypatch.setattr(
        live_runtime_module,
        "_ACTIVE_HANDOFF_POLICY",
        HandoffPolicy(policy=BLIND_RESET_V0),
    )
    blind_runtime = _make_runtime_class(Base)()
    blind_runtime.desired_twist(mode="path", path_time_s=0.0, internal_setpoint_n=5.0)

    assert blind_runtime._outer_state == _state(0.0, 0.0)
    receipt = blind_runtime._r013_handoff.receipt()
    assert receipt["policy"] == BLIND_RESET_V0
    assert receipt["handoff_mode"] == "blind_reset"
    assert receipt["reset_at_path_entry"] is True


def test_state21_sidecar_is_attempt_scoped_and_json_safe(tmp_path: Path) -> None:
    trace = State21BaselineTrace(tmp_path, persistence_stride=1)
    trace.begin_attempt(3)
    row = build_state21_row(
        monotonic_s=1.0,
        tcp_pose_m_rad=(0.0,) * 6,
        tcp_speed_m_s_rad_s=(0.0,) * 6,
        normal_load_n=0.4,
        force_norm_n=0.4,
        filtered_normal_n=0.4,
        torque_norm_nm=0.01,
        sensor_fresh=True,
        wrench=(0.0, 0.0, -0.4, 0.0, 0.0, 0.0),
        attempt_ordinal=3,
        qdot=(0.0,) * 6,
        force_integral_n_s=0.12,
        handoff_policy=FREEZE_CARRY_V1,
        baseline_transition={
            "profile_id": "r013-baseline-transition-v1",
            "path_request_allowed": True,
            "ramp_complete": True,
            "sensor_fresh": True,
            "timing_gate_passed": True,
            "safety_normal": True,
            "hard_limits_passed": True,
            "narrow_readiness_passed": False,
            "narrow_path_release_opened": False,
            "reason": "r013_hard_transition_open",
        },
    )
    trace.observe(row)
    trace.flush()
    trace.close()

    assert trace.attempt_rows(3)[0]["schema"] == STATE21_BASELINE_TRACE_SCHEMA
    values = [
        json.loads(line)
        for line in (tmp_path / "r013-state21-baseline-trace.jsonl").read_text().splitlines()
    ]
    assert len(values) == 1
    assert values[0]["attempt_ordinal"] == 3
    assert values[0]["qdot"] == [0.0] * 6
    assert values[0]["baseline_transition"] == {
        "profile_id": "r013-baseline-transition-v1",
        "path_request_allowed": True,
        "ramp_complete": True,
        "sensor_fresh": True,
        "timing_gate_passed": True,
        "safety_normal": True,
        "hard_limits_passed": True,
        "narrow_readiness_passed": False,
        "narrow_path_release_opened": False,
        "reason": "r013_hard_transition_open",
    }
