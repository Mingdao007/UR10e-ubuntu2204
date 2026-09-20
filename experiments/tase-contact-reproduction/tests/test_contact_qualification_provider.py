"""Offline tests for the explicit contact provider qualification seam."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
RUNTIME_SRC = Path(__file__).resolve().parents[3] / "src" / "ur10e_experiment_runtime"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(RUNTIME_SRC))

from step5d_autotune_v4_r004 import baseline_runtime  # noqa: E402
from step5d_autotune_v4_r004.qualification import (  # noqa: E402
    CanonicalQualificationControl,
    QualificationControlError,
)
from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE  # noqa: E402
from step5d_autotune_v4_r004.transport import R004OutputSnapshot  # noqa: E402
from step5d_autotune_v4_r004.wire import SensorPacket  # noqa: E402


@dataclass(frozen=True)
class CalibratedCommand:
    qdot: tuple[float, float, float, float, float, float]
    jacobian_6x6: tuple[tuple[float, ...], ...]
    observed_model_hashes: dict[str, str]
    tangential_error_m: tuple[float, float]
    orientation_error_rad: tuple[float, float, float]
    path_time_s: float
    solver_status: float


def _identity() -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(1.0 if row == column else 0.0 for column in range(6))
        for row in range(6)
    )


def _calibrated(qdot: tuple[float, ...] = (0.0001, 0.0, 0.0, 0.0, 0.0, 0.0)) -> CalibratedCommand:
    return CalibratedCommand(
        qdot=qdot,
        jacobian_6x6=_identity(),
        observed_model_hashes={},
        tangential_error_m=(0.0, 0.0),
        orientation_error_rad=(0.0, 0.0, 0.0),
        path_time_s=0.002,
        solver_status=40.0,
    )


def _output(*, state: int = 25) -> R004OutputSnapshot:
    return R004OutputSnapshot(
        observed_at_s=0.001,
        timestamp=0.001,
        payload_kg=0.413,
        payload_cog_m=(0.0011, 0.0031, 0.0163),
        tcp_offset_m_rad=(0.0, 0.0, 0.0874, 0.0, 0.0, 0.0),
        tcp_speed_m_s_rad_s=(0.0,) * 6,
        tcp_pose_m_rad=(0.0,) * 6,
        q_rad=(0.0,) * 6,
        qd_rad_s=(0.0,) * 6,
        safety_mode="NORMAL",
        robot_mode=7,
        runtime_state=2,
        consumed_packet_sequence=1,
        integer_echoes={26: state},
    )


def _sensor() -> SensorPacket:
    return SensorPacket(
        normal_load_n=4.0,
        force_norm_n=4.0,
        heartbeat=2.0,
        sensor_fresh=True,
        stop_request=False,
        eoat_get_ack=True,
        torque_norm_nm=0.0,
        wrench=(0.0,) * 6,
        filtered_normal_n=4.0,
        observed_at_s=0.001,
    )


class _PathController:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def step(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(filtered_normal_n=4.4)


class _Runtime:
    def __init__(self, *, fail_outer: bool = True) -> None:
        self.fail_outer = fail_outer
        self.path_errors_calls = 0
        self.desired_twist_calls = 0
        self.command_calls = 0

    def path_errors(self, **kwargs):
        self.path_errors_calls += 1
        if self.fail_outer:
            raise AssertionError("legacy path_errors was called")
        return (0.0, 0.0), (0.0, 0.0, 0.0)

    def desired_twist(self, **kwargs):
        self.desired_twist_calls += 1
        if self.fail_outer:
            raise AssertionError("legacy desired_twist was called")
        return (0.0001, 0.0, 0.0, 0.0, 0.0, 0.0)

    def command(self, **kwargs):
        self.command_calls += 1
        if self.fail_outer:
            raise AssertionError("legacy command was called")
        return _calibrated()


class _Provider:
    def __init__(self, *, qdot=(0.0001, 0.0, 0.0, 0.0, 0.0, 0.0)) -> None:
        self.qdot = tuple(qdot)
        self.path_errors_calls: list[dict[str, object]] = []
        self.command_calls: list[dict[str, object]] = []
        self.pause_calls: list[dict[str, object]] = []
        self.late_cycle_calls: list[dict[str, object]] = []
        self.last_result = None

    def path_errors(self, **kwargs):
        self.path_errors_calls.append(kwargs)
        return (0.0, 0.0), (0.0, 0.0, 0.0)

    def command(self, **kwargs):
        self.command_calls.append(kwargs)
        self.last_result = {"filtered_normal_n": 4.75}
        return _calibrated(self.qdot)

    def pause(self, **kwargs):
        self.pause_calls.append(kwargs)

    def hold_pre_path_late_cycle(self, **kwargs):
        self.late_cycle_calls.append(kwargs)
        return {
            "policy": "pre_path_late_cycle_evidence_only",
            "actual_dt_s": kwargs["actual_dt_s"],
            "filtered_normal_n": 4.75,
            "law_state_advanced": False,
            "readiness_filter_advanced": False,
            "native_law_dt_s": 0.002,
        }


def _successful_baseline(candidate, state, observation, **kwargs):
    return state, baseline_runtime.BaselineCommand(
        phase=state.phase,
        internal_setpoint_n=5.0,
        candidate_target_force_n=5.0,
        approach_speed_m_s=0.0,
        xy_velocity_m_s=(0.0, 0.0),
        angular_velocity_rad_s=(0.0, 0.0, 0.0),
        stop=False,
        retract_allowed=False,
        auto_home=False,
        reason="",
    )


def _control(provider, *, path_requested: bool = True) -> CanonicalQualificationControl:
    control = object.__new__(CanonicalQualificationControl)
    control.candidate = SimpleNamespace(motion_kp=1.0)
    control.attempt_id = "provider-test"
    control.release_contract = SimpleNamespace()
    control.path_requested = path_requested
    control.motion_profile = None
    control.canonical_runtime_only = False
    control.force_integral_limit_n_s = 1.0
    control.r013_baseline_transition_profile = None
    control.contact_command_provider = provider
    control._setpoint_n = 5.0
    control._sticky_latched = 0
    control._last_monotonic_s = 0.0
    control._origin_monotonic_s = 0.0
    control._contract = SimpleNamespace(model_hashes={})
    control._canonical_candidate = object()
    control._runtime = _Runtime()
    control._path_controller = _PathController()
    control._baseline_state = baseline_runtime.BaselineState(
        phase=baseline_runtime.BaselinePhase.SUCCESS
    )
    control._timing = SimpleNamespace(
        observe=lambda elapsed: 0.002,
        stopped=False,
        stop_reason="",
    )
    control._startup = SimpleNamespace(
        observe=lambda heartbeat, elapsed: True,
        stopped=False,
        stop_reason="",
    )
    control._startup_ready_latched = True
    control._path_origin_monotonic_s = 0.0
    control._tangential_tolerance_m_s = 2e-6
    control._angular_tolerance_rad_s = 2e-6
    control._required_hold_s = 10.0
    control._readiness_gate = baseline_runtime.BaselineReadinessGate()
    control._path_entry_release_gate = baseline_runtime.PathEntryReleaseGate()
    control._path_entry_release_state = baseline_runtime.PathEntryReleaseState(
        dwell_s=0.5,
        opened=True,
    )
    control._qualification_retract_issued = False
    control._previous_qdot = (0.0,) * 6
    control._tube_cbf = None
    control._tube_cbf_init_failed = True
    control.last_tube_cbf = None
    control._path_entry_rate_limit = None
    control._path_entry_rate_limit_init_failed = True
    control.last_path_entry_rate_limit = None
    control.last_baseline_transition = None
    control.last_baseline_residual = None
    return control


def _run(control, monkeypatch, *, state: int = 25):
    monkeypatch.setattr(baseline_runtime, "step_baseline", _successful_baseline)
    calibrated_runtime_stub = SimpleNamespace(CalibratedCommand=CalibratedCommand)
    monkeypatch.setitem(
        sys.modules,
        "step5d_autotune_v4_r004.calibrated_runtime",
        calibrated_runtime_stub,
    )
    return control.step(
        output=_output(state=state),
        sensor=_sensor(),
        monotonic_s=0.002,
        command_sequence=1,
    )


def test_contact_provider_bypasses_legacy_outer_and_post_qp_transforms(monkeypatch) -> None:
    provider = _Provider()
    control = _control(provider)

    class _RaisingRamp:
        def apply(self, **kwargs):
            raise AssertionError("legacy path-entry ramp was called")

    class _RaisingTube:
        def apply(self, *args, **kwargs):
            raise AssertionError("legacy Tube+CBF was called")

    control._path_entry_rate_limit = _RaisingRamp()
    control._tube_cbf = _RaisingTube()

    import step5d_autotune_v4_r004.runtime as runtime_module

    monkeypatch.setattr(
        runtime_module,
        "project_qdot_to_gate",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("project_qdot was called")),
    )
    monkeypatch.setattr(
        runtime_module,
        "rescale_qdot_to_gate",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("rescale_qdot was called")),
    )

    result = _run(control, monkeypatch)

    assert result.qdot == pytest.approx(provider.qdot)
    assert result.filtered_normal_n == pytest.approx(4.75)
    assert len(provider.command_calls) == 1
    assert len(control._path_controller.calls) == 1
    assert len(provider.path_errors_calls) == 1
    assert control._runtime.path_errors_calls == 0
    assert control._runtime.desired_twist_calls == 0
    assert control._runtime.command_calls == 0
    assert control.last_path_entry_rate_limit is None
    assert control.last_tube_cbf is None
    assert control.last_baseline_residual is None
    assert provider.command_calls[0]["actual_dt_s"] == pytest.approx(0.002)
    assert provider.command_calls[0]["mode"] == "path"


def test_contact_provider_guard_rejects_command_without_rescaling(monkeypatch) -> None:
    provider = _Provider(qdot=(0.01, 0.0, 0.0, 0.0, 0.0, 0.0))
    control = _control(provider)

    with pytest.raises(QualificationControlError, match="provider output exceeds invariant envelope"):
        _run(control, monkeypatch)
    assert len(provider.command_calls) == 1
    assert control._runtime.desired_twist_calls == 0
    assert control._previous_qdot == (0.0,) * 6


def test_contact_provider_preserves_mandatory_host_slew_without_scaling(monkeypatch) -> None:
    delta_limit = R004_MOTION_PROFILE.host_slew_rad_s2 * min(0.002, 0.02)
    provider = _Provider(qdot=(2.0 * delta_limit, 0.0, 0.0, 0.0, 0.0, 0.0))
    control = _control(provider)
    control.motion_profile = R004_MOTION_PROFILE

    with pytest.raises(QualificationControlError, match="mandatory host slew limit"):
        _run(control, monkeypatch)
    assert provider.command_calls
    assert control._previous_qdot == (0.0,) * 6


def test_contact_provider_is_not_called_at_state21_zero_command_seam(monkeypatch) -> None:
    provider = _Provider()
    control = _control(provider)

    result = _run(control, monkeypatch, state=21)

    assert result.qdot == (0.0,) * 6
    assert result.canonical_reason == "path_entry_release_open"
    assert provider.command_calls == []
    assert provider.path_errors_calls == []
    assert len(provider.pause_calls) == 1
    assert provider.pause_calls[0]["reason"] == "path_entry_release_open"


def test_contact_provider_late_cycle_bypasses_readiness_and_returns_zero_hold(monkeypatch) -> None:
    provider = _Provider()
    control = _control(provider)
    control._baseline_state = baseline_runtime.BaselineState(
        phase=baseline_runtime.BaselinePhase.RAMP
    )
    control._last_monotonic_s = 0.002
    control._origin_monotonic_s = 0.0
    control._setpoint_n = 3.0

    result = control.step(
        output=_output(state=21),
        sensor=_sensor(),
        monotonic_s=0.0135,
        command_sequence=2,
    )

    assert result.command_mode.value == 1
    assert result.qdot == (0.0,) * 6
    assert result.canonical_phase == "late_cycle"
    assert result.canonical_reason == "pre_path_late_cycle_evidence_only"
    assert result.actual_dt_s == pytest.approx(0.0115)
    assert result.internal_setpoint_n == pytest.approx(3.0)
    assert result.late_cycle is True
    assert result.native_law_dt_s == pytest.approx(0.002)
    assert provider.command_calls == []
    assert provider.pause_calls == []
    assert len(provider.late_cycle_calls) == 1
    assert provider.late_cycle_calls[0]["actual_dt_s"] == pytest.approx(0.0115)
    assert control._path_controller.calls == []
    assert control._previous_qdot == (0.0,) * 6


def test_contact_provider_pauses_while_path_release_dwell_is_pending(monkeypatch) -> None:
    provider = _Provider()
    control = _control(provider)
    control._path_entry_release_state = baseline_runtime.PathEntryReleaseState()

    result = _run(control, monkeypatch)

    assert result.qdot == (0.0,) * 6
    assert result.canonical_reason == "path_entry_release_dwell_pending"
    assert provider.command_calls == []
    assert len(provider.pause_calls) == 1
    assert provider.pause_calls[0]["reason"] == "path_entry_release_dwell_pending"


def test_contact_provider_pauses_during_startup_without_command(monkeypatch) -> None:
    provider = _Provider()
    control = _control(provider)
    control._startup_ready_latched = False
    control._startup = SimpleNamespace(
        observe=lambda heartbeat, elapsed: False,
        stopped=False,
        stop_reason="",
    )

    result = _run(control, monkeypatch)

    assert result.qdot == (0.0,) * 6
    assert result.canonical_phase == "startup"
    assert provider.command_calls == []
    assert len(provider.pause_calls) == 1
    assert provider.pause_calls[0]["reason"] == "startup_two_increments_pending"


def test_none_provider_keeps_legacy_runtime_path(monkeypatch) -> None:
    control = _control(None)
    control._runtime = _Runtime(fail_outer=False)

    result = _run(control, monkeypatch)

    assert result.qdot == pytest.approx((0.0001, 0.0, 0.0, 0.0, 0.0, 0.0))
    assert control._runtime.path_errors_calls == 1
    assert control._runtime.desired_twist_calls == 1
    assert control._runtime.command_calls == 1


def test_contact_provider_argument_is_optional_and_keyword_only() -> None:
    import inspect

    parameter = inspect.signature(CanonicalQualificationControl).parameters[
        "contact_command_provider"
    ]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
