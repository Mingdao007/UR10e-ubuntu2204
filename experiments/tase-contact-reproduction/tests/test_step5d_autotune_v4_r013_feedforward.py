"""Offline contract tests for the R013 manual demo A/B limb."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / "src" / "ur10e_experiment_runtime"))

import numpy as np
import pytest

from step5d_autotune_v4_r004 import calibrated_runtime as calibrated
from step5d_autotune_v4_r013.demo_profile import (
    HISTORICAL_BEST_CANDIDATE_TOKEN,
    demo_identity_sha256,
    demo_profile,
)
from step5d_autotune_v4_r013.feedforward import (
    FeedforwardMode,
    FeedforwardProfile,
    MotionAdmissionProfileV1,
)
from step5d_autotune_v4_r013.baseline_policy import (
    R013BaselineResidualPolicyV1,
    R013BaselineTransitionProfileV1,
    ResidualDisposition,
)
from step5d_autotune_v4_r013.campaign_config import (
    _bind_materialized_r013_profiles,
    controller_source_identity_sha256,
    r013_host_source_closure,
)
from step5d_autotune_v4_r013 import campaign_config as campaign_config_module
from step5d_autotune_v4_r013.identity import (
    CampaignFingerprint,
    CampaignIdentityError,
    LEGACY_PROFILE_IDENTITY,
    bind_r013_profile_identities,
    require_r013_profile_binding,
)
from step5d_autotune_v4_r004.baseline_runtime import BaselineObservation, BaselineState
from step5d_autotune_v4_r004.contracts import load_contract as load_r004_contract
from step5d_autotune_v4.contracts import load_contract as load_v4_contract
from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE
from step5d_autotune_v4_r004.transport import R004OutputSnapshot
from step5d_autotune_v4_r004.wire import CommandMode, SensorPacket
from step5d_autotune_v4_r006 import live_adapter as r006_live
from step5d_autotune_v4_r006.live_adapter import R006Candidate
from step5d_autotune_v4_r006.motion_profile import (
    ACTIVE_MOTION_ENVELOPE_V2,
    r006_runtime_path_reference,
)
from run_step5d_autotune_v4_r013_demo import _bind_demo_identity
from run_step5d_autotune_v4_r013_live import (
    LEGACY_PREPARED_CONFIG_MODE,
    _parse_args,
)
from step5d_autotune_v4_r013.live_owner import (
    R013OwnerError,
    _require_current_r013_source_identity,
    _r013_profile_binding_enabled,
)
from step5d_autotune_v4_r013 import live_owner as live_owner_module
from step5d_autotune_v4_r013.domain import KI_LATTICE_ANCHOR


def _runtime() -> calibrated.V4CalibratedRuntime:
    runtime = object.__new__(calibrated.V4CalibratedRuntime)
    runtime._feedforward_enabled = True
    runtime.candidate = SimpleNamespace(motion_kp=2.0, orientation_ko=0.1)
    runtime.motion_profile = None
    runtime.force_integral_limit_n_s = 1.0
    runtime._outer_state = SimpleNamespace()
    return runtime


def _reference(*_args: object, **_kwargs: object) -> dict[str, tuple[float, float]]:
    return {
        "path_error_xy": (0.1, -0.2),
        "desired_velocity_xy": (0.4, -0.6),
        "desired_xy": (0.3, 0.4),
    }


def test_feedforward_profile_is_explicit_and_comparison_only() -> None:
    on = FeedforwardProfile.from_value("on")
    off = FeedforwardProfile.from_value("off")

    assert on.mode is FeedforwardMode.ON
    assert on.enabled is True
    assert off.mode is FeedforwardMode.OFF
    assert off.enabled is False
    assert off.as_dict()["same_candidate"] is True
    assert off.as_dict()["same_path_and_safety"] is True
    assert len(off.as_dict()["disabled_terms"]) == 2

    runtime = _runtime()
    with pytest.raises(AttributeError):
        runtime.feedforward_enabled = False

    profile = demo_profile("off")
    assert profile["candidate_token"] == HISTORICAL_BEST_CANDIDATE_TOKEN
    assert "0.49 N" in profile["historical_comparison_note"]
    assert profile["optimizer"] == "disabled_fixed_candidate"
    assert demo_identity_sha256(
        feedforward_mode="on", campaign_fingerprint_sha256="a" * 64
    ) != demo_identity_sha256(
        feedforward_mode="off", campaign_fingerprint_sha256="a" * 64
    )


def test_continuous_runner_accepts_explicit_no_ff_legacy_profile() -> None:
    args = _parse_args(
        [
            "--run-dir",
            "/tmp/r013-no-ff",
            "--launch-profile",
            "/tmp/launch.json",
            "--feedforward",
            "off",
            "--campaign-config-mode",
            LEGACY_PREPARED_CONFIG_MODE,
        ]
    )
    assert args.feedforward == "off"
    assert args.campaign_config_mode == LEGACY_PREPARED_CONFIG_MODE


def test_path_errors_disable_only_the_velocity_feedforward_term(monkeypatch) -> None:
    monkeypatch.setattr(calibrated, "step5_path_reference", _reference)
    monkeypatch.setattr(calibrated, "rotvec_to_matrix", lambda _value: np.eye(3))
    runtime = _runtime()
    pose = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    on, _ = runtime.path_errors(actual_tcp_pose=pose, path_time_s=0.0, motion_kp=2.0)
    runtime._feedforward_enabled = False
    off, _ = runtime.path_errors(actual_tcp_pose=pose, path_time_s=0.0, motion_kp=2.0)

    assert on == (0.30000000000000004, -0.5)
    assert off == (0.1, -0.2)


def test_outer_loop_desired_velocity_is_disabled_with_the_same_mode(monkeypatch) -> None:
    monkeypatch.setattr(calibrated, "step5_path_reference", _reference)
    monkeypatch.setattr(calibrated, "rotvec_to_matrix", lambda _value: np.eye(3))
    monkeypatch.setattr(
        calibrated,
        "derive_force_terms",
        lambda _candidate: {"kf": 0.1, "Md": 1.0, "Bd": 1.0},
    )
    captured: list[object] = []

    def fake_compute(_config, state, inputs, **_kwargs):
        captured.append(inputs)
        return SimpleNamespace(next_state=state, xdot_c=(0.0,) * 6, diagnostics={})

    monkeypatch.setattr(calibrated, "compute_step5d_outer_loop", fake_compute)
    runtime = _runtime()
    kwargs = {
        "actual_tcp_pose": (0.0,) * 6,
        "actual_tcp_speed": (0.0,) * 6,
        "force_tcp_n": (0.0, 0.0, 5.0),
        "filtered_normal_n": 5.0,
        "internal_setpoint_n": 5.0,
        "actual_dt_s": 0.01,
        "mode": "path",
        "path_time_s": 1.0,
    }

    runtime.desired_twist(**kwargs)
    on_inputs = captured[-1]
    runtime._feedforward_enabled = False
    runtime.desired_twist(**kwargs)
    off_inputs = captured[-1]

    assert on_inputs.xdot_pd_base[:2] == (0.4, -0.6)
    assert off_inputs.xdot_pd_base[:2] == (0.0, 0.0)


def test_demo_identity_rejects_mixing_the_two_modes(tmp_path) -> None:
    base = {
        "demo_identity_sha256": "a" * 64,
        "campaign_id": "campaign",
        "run_id": "run",
        "attempt_id": "attempt",
        "campaign_fingerprint_sha256": "b" * 64,
        "feedforward_mode": "on",
        "candidate_token": HISTORICAL_BEST_CANDIDATE_TOKEN,
    }
    path = tmp_path / "r013_demo_identity.json"
    first = _bind_demo_identity(path=path, receipt=base, action="start")
    assert first["state"] == "selection_bound"
    same = _bind_demo_identity(path=path, receipt=base, action="home")
    assert same["state"] == "selection_bound"

    other = dict(base)
    other["demo_identity_sha256"] = "c" * 64
    other["feedforward_mode"] = "off"
    with pytest.raises(SystemExit, match="other feedforward mode"):
        _bind_demo_identity(path=path, receipt=other, action="start")


def test_r013_offline_residual_transition_admission_and_identity_contracts() -> None:
    contract = load_v4_contract(runtime_only=True)
    jacobian = (
        (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    )
    projection = R013BaselineResidualPolicyV1().apply(
        contract=contract,
        strict_qdot=(0.0, 0.2, 0.3, 0.01, 0.0, 0.0),
        jacobian_6x6=jacobian,
        normal_base=(0.0, 0.0, 1.0),
        previous_qdot=(0.0,) * 6,
        actual_dt_s=0.002,
        observed_model_hashes=contract.model_hashes,
        motion_profile=R004_MOTION_PROFILE,
    )
    assert projection.disposition is ResidualDisposition.PROJECTED
    assert any(abs(value) > 0.0 for value in projection.qdot)
    assert projection.tangential_m_s <= 2e-6
    assert projection.angular_rad_s <= 2e-6

    # Regression fixture from the failed 2026-08-17 R013 state-21 posture.
    # Use the calibrated UR10e Jacobian, not the identity-matrix unit seam,
    # so this proves that the residual projection remains nonzero and inside
    # the unchanged V4 gate at the physical IK branch that previously held.
    actual_q = (
        0.6282092928886414,
        -1.8694631061949671,
        -2.5496318340301514,
        -0.26894088209185796,
        1.5299158096313477,
        -0.9407718817340296,
    )
    actual_jacobian = calibrated.tcp_jacobian_base(
        calibrated.build_calibrated_model(), actual_q
    )
    replay_projection = R013BaselineResidualPolicyV1().apply(
        contract=contract,
        strict_qdot=(
            4.5160841413143407e-07,
            5.319754745704875e-04,
            3.840427981133045e-05,
            -5.666945472946493e-04,
            2.3949415320199947e-06,
            -5.782886145379583e-07,
        ),
        jacobian_6x6=actual_jacobian,
        normal_base=(0.0, 0.0, 1.0),
        previous_qdot=(0.0,) * 6,
        actual_dt_s=0.002,
        observed_model_hashes=contract.model_hashes,
        motion_profile=R004_MOTION_PROFILE,
    )
    assert replay_projection.disposition is ResidualDisposition.PROJECTED
    assert any(abs(value) > 0.0 for value in replay_projection.qdot)
    assert replay_projection.tangential_m_s <= 2e-6
    assert replay_projection.angular_rad_s <= 2e-6

    transition = R013BaselineTransitionProfileV1().evaluate(
        baseline_state=BaselineState(after_latch_s=8.0),
        observation=BaselineObservation(
            dt_s=0.002,
            one_newton_latched=True,
            filtered_normal_n=3.7,
            raw_normal_n=3.8,
            force_norm_n=4.0,
            torque_norm_nm=0.1,
            sensor_fresh=True,
            stationary=False,
        ),
        timing_gate_passed=True,
        safety_normal=True,
        narrow_readiness_passed=False,
        narrow_path_release_opened=False,
    )
    assert transition.path_request_allowed is True
    assert transition.narrow_readiness_passed is False
    assert transition.narrow_path_release_opened is False

    invalid = R013BaselineResidualPolicyV1().apply(
        contract=contract,
        strict_qdot=(0.0, 0.2, 0.3, 0.01, 0.0, 0.0),
        jacobian_6x6=(
            (0.0,) * 6,
            (0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        ),
        normal_base=(0.0, 0.0, 1.0),
        previous_qdot=(0.0,) * 6,
        actual_dt_s=0.002,
        observed_model_hashes=contract.model_hashes,
        motion_profile=R004_MOTION_PROFILE,
    )
    assert invalid.disposition is ResidualDisposition.ZERO_FAIL_CLOSED

    result = SimpleNamespace(
        duration_s=60.0,
        motion_gate=False,
        safe_return=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        timing_gate=True,
        identity_gate=True,
        metrics={
            "path_phase": 6,
            "qd_correlation": 0.98,
            "qd_lag_s": 0.012,
            "r013_force_lifecycle_complete": True,
            "hard_tube_passed": True,
            "soft_tube_passed": True,
            "xy_error_p95_m": 0.00118,
            "xy_error_max_m": 0.00126,
            "endpoint_error_max_m": 0.00018,
        },
    )
    off = FeedforwardProfile.from_value("off")
    off_admission = MotionAdmissionProfileV1.from_feedforward(off)
    assert off_admission.evaluate(result).accepted is True
    assert MotionAdmissionProfileV1.from_feedforward(
        FeedforwardProfile.from_value("on")
    ).evaluate(result).accepted is False

    base = CampaignFingerprint.legacy_default(
        handoff_policy="freeze_carry_v1", runtime_strategy_identity="legacy"
    )
    off_bound = bind_r013_profile_identities(
        base,
        feedforward_profile=off,
        motion_admission_profile=off_admission,
        baseline_transition_profile=R013BaselineTransitionProfileV1(),
        baseline_residual_policy=R013BaselineResidualPolicyV1(),
    )
    assert _bind_materialized_r013_profiles(base, off) == off_bound
    assert off_bound.feedforward_profile_identity == off.profile_id
    assert off_bound.motion_admission_profile_identity == off_admission.profile_id
    assert off_bound.baseline_transition_profile_identity != LEGACY_PROFILE_IDENTITY
    assert off_bound.baseline_residual_policy_identity != LEGACY_PROFILE_IDENTITY

    on = FeedforwardProfile.from_value("on")
    on_admission = MotionAdmissionProfileV1.from_feedforward(on)
    assert _r013_profile_binding_enabled(
        fingerprint=base,
        feedforward_profile=on,
        motion_admission_profile=on_admission,
        baseline_transition_profile=R013BaselineTransitionProfileV1(),
        baseline_residual_policy=R013BaselineResidualPolicyV1(),
    ) is False
    with pytest.raises(R013OwnerError, match="FF-off requires"):
        _r013_profile_binding_enabled(
            fingerprint=base,
            feedforward_profile=off,
            motion_admission_profile=off_admission,
            baseline_transition_profile=R013BaselineTransitionProfileV1(),
            baseline_residual_policy=R013BaselineResidualPolicyV1(),
        )
    partial = base.with_r013_profiles(
        feedforward_profile_identity=on.profile_id,
        motion_admission_profile_identity=LEGACY_PROFILE_IDENTITY,
        baseline_transition_profile_identity=LEGACY_PROFILE_IDENTITY,
        baseline_residual_policy_identity=LEGACY_PROFILE_IDENTITY,
    )
    with pytest.raises(R013OwnerError, match="profile fingerprint differs"):
        _r013_profile_binding_enabled(
            fingerprint=partial,
            feedforward_profile=on,
            motion_admission_profile=on_admission,
            baseline_transition_profile=R013BaselineTransitionProfileV1(),
            baseline_residual_policy=R013BaselineResidualPolicyV1(),
        )
    with pytest.raises(R013OwnerError, match="profile fingerprint differs"):
        _r013_profile_binding_enabled(
            fingerprint=off_bound,
            feedforward_profile=on,
            motion_admission_profile=on_admission,
            baseline_transition_profile=R013BaselineTransitionProfileV1(),
            baseline_residual_policy=R013BaselineResidualPolicyV1(),
        )
    with pytest.raises(CampaignIdentityError):
        require_r013_profile_binding(
            off_bound,
            feedforward_profile=FeedforwardProfile.from_value("on"),
            motion_admission_profile=MotionAdmissionProfileV1.from_feedforward(
                FeedforwardProfile.from_value("on")
            ),
            baseline_transition_profile=R013BaselineTransitionProfileV1(),
            baseline_residual_policy=R013BaselineResidualPolicyV1(),
        )


def test_r013_prepared_control_transition_is_a_one_shot_qualification_edge() -> None:
    """Reproduce the complete R006 factory -> R004 control seam from live 002.

    The live failure reached the R013 hard transition after the 8 s ramp, but
    the transition rewrite reset ``retract_allowed`` on every later tick.  A
    stationary qualification therefore remained in BASELINE until the TP's
    independent 30 s watchdog stopped it with reason 48.  This fixture keeps
    force outside the narrow readiness window and proves that the opt-in R013
    transition still emits exactly one zero-qdot boundary tick followed by
    RETRACT, using the same prepared-control factory consumed in production.
    """

    candidate_values = demo_profile("off")["candidate"]
    candidate = R006Candidate.from_canonical(
        {
            **candidate_values,
            "force_i_gain": KI_LATTICE_ANCHOR,
            "i_off": False,
        }
    )
    release_contract = load_r004_contract()
    transition_profile = R013BaselineTransitionProfileV1()
    injection = r006_live._R006ScopedRuntimeInjection(
        motion_profile=ACTIVE_MOTION_ENVELOPE_V2.mature_profile,
        path_reference=r006_runtime_path_reference,
        qualification_profile=transition_profile,
    )
    injection.activate()
    try:
        prepared = injection.prepare_control(
            candidate=candidate,
            attempt_id="r013-live-002-e1-o1",
            release_contract=release_contract,
            path_requested=False,
            canonical_runtime_only=True,
        )
        consumed = r006_live.r004_writer_module.CanonicalQualificationControl(
            candidate,
            attempt_id="r013-live-002-e1-o1",
            release_contract=release_contract,
            path_requested=False,
            canonical_runtime_only=True,
        )
        assert consumed is prepared
        assert consumed.r013_baseline_transition_profile is transition_profile

        output = R004OutputSnapshot(
            observed_at_s=0.0,
            timestamp=0.0,
            payload_kg=1.0,
            payload_cog_m=(0.0, 0.0, 0.0),
            tcp_offset_m_rad=(0.0,) * 6,
            tcp_speed_m_s_rad_s=(0.0,) * 6,
            tcp_pose_m_rad=(0.0,) * 6,
            q_rad=(0.0,) * 6,
            qd_rad_s=(0.0,) * 6,
            safety_mode=1,
            robot_mode=7,
            runtime_state=2,
            consumed_packet_sequence=0,
            integer_echoes={
                24: 1,
                25: 1,
                26: 21,
                27: 1,
                28: 0,
                29: 1,
                30: 1,
                31: 1,
                32: 606006,
                33: 13,
                34: 613013,
            },
        )
        commands = []
        for sample_index in range(4_005):
            sensor = SensorPacket(
                normal_load_n=2.9,
                force_norm_n=3.0,
                heartbeat=float(sample_index),
                sensor_fresh=True,
                stop_request=False,
                eoat_get_ack=True,
                torque_norm_nm=0.01,
                wrench=(0.0, 0.0, -2.9, 0.0, 0.0, 0.0),
                filtered_normal_n=2.9,
            )
            commands.append(
                consumed.step(
                    output=output,
                    sensor=sensor,
                    monotonic_s=sample_index * 0.002,
                    command_sequence=sample_index,
                )
            )
            if commands[-1].command_mode is CommandMode.RETRACT:
                break

        assert commands[-2].command_mode is CommandMode.BASELINE
        assert commands[-2].canonical_phase == "success_wait_stationary"
        assert commands[-2].canonical_reason == "stationary_retract_gate_pending"
        assert commands[-1].command_mode is CommandMode.RETRACT
        assert commands[-1].canonical_phase == "success"
        assert consumed.last_baseline_transition["path_request_allowed"] is True
        assert consumed.last_baseline_transition["narrow_readiness_passed"] is False
    finally:
        injection.deactivate()


def test_r013_prepared_control_rejects_a_dropped_profile_before_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_values = demo_profile("off")["candidate"]
    candidate = R006Candidate.from_canonical(
        {
            **candidate_values,
            "force_i_gain": KI_LATTICE_ANCHOR,
            "i_off": False,
        }
    )
    transition_profile = R013BaselineTransitionProfileV1()

    class DropsProfile:
        def __init__(self, _candidate: object, **_kwargs: object) -> None:
            self.r013_baseline_transition_profile = None

    monkeypatch.setattr(
        r006_live,
        "_R006NativeCanonicalQualificationControl",
        DropsProfile,
    )
    injection = r006_live._R006ScopedRuntimeInjection(
        motion_profile=ACTIVE_MOTION_ENVELOPE_V2.mature_profile,
        path_reference=r006_runtime_path_reference,
        qualification_profile=transition_profile,
    )
    injection.activate()
    try:
        with pytest.raises(
            r006_live.R006LiveAdapterError,
            match="qualification profile binding differs",
        ):
            injection.prepare_control(
                candidate=candidate,
                attempt_id="r013-dropped-profile-e1-o1",
                release_contract=load_r004_contract(),
                path_requested=False,
                canonical_runtime_only=True,
            )
        assert injection._prepared_control is None
        assert injection._prepared_key is None
    finally:
        injection.deactivate()


def test_r013_source_identity_changes_with_host_behavior_and_live_rejects_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative = "tools/r013_behavior_probe.py"
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(b"transition = 'before'\n")
    monkeypatch.setattr(
        campaign_config_module,
        "R013_HOST_SOURCE_PATHS",
        (relative,),
    )
    triplet = {"script": "a" * 64, "txt": "b" * 64, "urp": "c" * 64}

    first_closure = r013_host_source_closure(source_root=tmp_path)
    first_identity = controller_source_identity_sha256(
        triplet,
        source_root=tmp_path,
    )
    source.write_bytes(b"transition = 'after'\n")
    second_closure = r013_host_source_closure(source_root=tmp_path)
    second_identity = controller_source_identity_sha256(
        triplet,
        source_root=tmp_path,
    )

    assert first_closure["sha256"] != second_closure["sha256"]
    assert first_identity != second_identity

    fingerprint = CampaignFingerprint.legacy_default(
        handoff_policy="freeze_carry_v1",
        runtime_strategy_identity="legacy",
    )
    fingerprint = fingerprint.__class__(
        **{
            **fingerprint.as_dict(),
            "source_identity": first_identity,
        }
    )
    monkeypatch.setattr(
        live_owner_module,
        "controller_source_identity_sha256",
        lambda _triplet: second_identity,
    )
    with pytest.raises(R013OwnerError, match="current host/controller source identity differs"):
        _require_current_r013_source_identity(
            fingerprint=fingerprint,
            controller_triplet_sha256=triplet,
        )


def test_r013_source_closure_covers_v4_and_v5_authority_surface() -> None:
    required = {
        "tools/step5c_strict_rnn.py",
        "tools/step5d_paper_outer_loop.py",
        "tools/contact_semantics.py",
        "tools/run_v4_stage_live.py",
        "tools/step5d_autotune_v4_r013/v4_two_stage_campaign.py",
        "tools/step6_figure8_autotune_v1/v5_live_owner.py",
        "tools/step6_figure8_autotune_v1/v5_live_runtime.py",
        "tools/step6_figure8_autotune_v1/v5_campaign_runner.py",
        "tools/step6_figure8_autotune_v1/v5_rollover.py",
    }
    assert required.issubset(set(campaign_config_module.R013_HOST_SOURCE_PATHS))
    closure = r013_host_source_closure()
    assert required.issubset(set(closure["files"]))
