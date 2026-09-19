"""Offline measured-observation adapter admission and full lifecycle checks."""
import json
from dataclasses import replace
from pathlib import Path
import numpy as np
import pytest
from test_contact_benchmark_runtime import lib
from test_contact_benchmark_provider import inputs
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from contact_benchmark_kernel import KernelDeadlineError
from yield_contact_runtime import YieldContactRuntime
from yield_contact_provider import YieldContactProvider
from contact_yield_controller import YieldControllerError

ROOT = Path(__file__).resolve().parents[1]


def setup(lib):
    receipt = json.loads((ROOT/'report/contact-six-qp-20260917/preserved-home.json').read_text())
    robot = dict(receipt['rtde'])
    robot.update(actual_q=receipt['home_q'], actual_qd=[0.]*6,
                 actual_TCP_pose=receipt['home_pose'], observed_at_s=100., timestamp=1234.)
    rotation = rotvec_to_matrix(np.array(receipt['home_pose'][3:]))
    runtime = YieldContactRuntime(method='MSFC', qp_library=lib,
        anchor_m=receipt['home_pose'][:3], task_basis=rotation,
        approach_inward_base=rotation[:, 2], deadline_s=None)
    output, sensor = inputs(robot)
    provider = YieldContactProvider(runtime=runtime, model_hashes={'calibration':runtime.model.calibration_hash})
    return runtime, provider, output, sensor


def call(provider, output, sensor, tick, mode='baseline', **kwargs):
    now = 100. + tick*.002
    output.received_monotonic_s = now
    output.timestamp = 1234. + tick*.002
    return provider.command(output=output, sensor=replace(sensor, observed_at_s=now),
        monotonic_s=now, actual_dt_s=.002, mode=mode, internal_setpoint_n=kwargs.pop('force',5.), **kwargs)


def test_full_ramp_stationary_seam_entry_path_and_memory_carry(lib):
    runtime, provider, output, sensor = setup(lib)
    with runtime:
        for tick, force in enumerate((1.,2.,3.,4.,5.)):
            call(provider,output,sensor,tick,force=force)
        before = runtime.controller.snapshot()
        now = 100.010
        output.received_monotonic_s=now; output.timestamp=1234.010
        provider.pause(output=output,sensor=replace(sensor,observed_at_s=now),
            monotonic_s=now,actual_dt_s=.002,reason='stationary seam')
        after = runtime.controller.snapshot()
        assert after != before
        before['time_s'] = after['time_s']
        assert before == after
        for i in range(500):
            call(provider,output,sensor,i+6,mode='entry',entry_time_s=i*.002)
        command = call(provider,output,sensor,506,mode='path',path_time_s=0.)
        assert command.qdot == provider.last_result['qdot_rad_s']
        assert provider.last_result['formal_time_s'] == 0.
        assert runtime.paused_s == pytest.approx(.002)
        call(provider,output,sensor,507,mode='path',path_time_s=.002)
        state = runtime.snapshot()
        with pytest.raises(ValueError,match='return'):
            call(provider,output,sensor,508)
        assert state == runtime.snapshot()


def test_missing_entry_clock_jump_and_wrong_force_fail_without_state_change(lib):
    runtime, provider, output, sensor = setup(lib)
    with runtime:
        call(provider,output,sensor,0,force=1.)
        before=runtime.snapshot()
        with pytest.raises(ValueError,match='completed entry'):
            call(provider,output,sensor,1,mode='path',path_time_s=0.)
        assert runtime.snapshot()==before
        call(provider,output,sensor,1,mode='entry',entry_time_s=0.)
        before=runtime.snapshot()
        with pytest.raises(ValueError,match='actual dt'):
            call(provider,output,sensor,2,mode='entry',entry_time_s=1.)
        assert runtime.snapshot()==before
        with pytest.raises(YieldControllerError,match='pre-PATH force'):
            call(provider,output,sensor,2,mode='entry',entry_time_s=.002,force=6.)
        assert runtime.snapshot()==before


def test_freshness_raw_guard_identity_deadline_and_evolving_orientation(lib):
    runtime, provider, output, sensor = setup(lib)
    with runtime:
        call(provider,output,sensor,0)
        before=runtime.snapshot()
        output.received_monotonic_s=99.9
        with pytest.raises(ValueError,match='80ms'):
            provider.command(output=output,sensor=sensor,monotonic_s=100.002,
                actual_dt_s=.002,mode='baseline',internal_setpoint_n=5.)
        assert runtime.snapshot()==before
        assert output.received_monotonic_s==99.9
        with pytest.raises(ValueError,match='raw sensor'):
            call(provider,output,replace(sensor,wrench=(0,0,-21,0,0,0)),1)
        output.payload_kg=1.
        with pytest.raises(ValueError,match='tool binding'):
            call(provider,output,sensor,1)
        output.payload_kg=.413
        runtime.deadline_s=1e-12
        with pytest.raises(KernelDeadlineError):call(provider,output,sensor,1)
        assert runtime.snapshot()==before
        rotation=rotvec_to_matrix(np.array(output.tcp_pose_m_rad[3:]))
        initial=np.asarray(runtime.orientation_error_rad(rotation))
        n=runtime.controller.estimator.normal.copy()
        n=n+.05*rotation[:,0];n/=np.linalg.norm(n)
        runtime.controller.estimator.normal=n
        changed=np.asarray(runtime.orientation_error_rad(rotation))
        assert np.linalg.norm(changed-initial)>.01


def test_task_basis_rejects_unit_determinant_shear(lib):
    receipt=json.loads((ROOT/'report/contact-six-qp-20260917/preserved-home.json').read_text())
    with pytest.raises(ValueError,match='orthonormal'):
        YieldContactRuntime(method='SFC',qp_library=lib,anchor_m=receipt['home_pose'][:3],
            task_basis=[[1,.2,0],[0,1,0],[0,0,1]],approach_inward_base=[0,0,1])


def test_first_interval_cannot_be_silently_replaced(lib):
    runtime,provider,output,sensor=setup(lib)
    with runtime:
        before=runtime.snapshot()
        with pytest.raises(ValueError,match='first sample interval'):
            provider.command(output=output,sensor=sensor,monotonic_s=100.,
                actual_dt_s=.003,mode='baseline',internal_setpoint_n=1.)
        assert before==runtime.snapshot()


def test_irregular_entry_boundary_does_not_freeze_or_invent_zero(lib):
    runtime,provider,output,sensor=setup(lib)
    with runtime:
        call(provider,output,sensor,0)
        call(provider,output,sensor,1,mode='entry',entry_time_s=0.)
        # 3 ms acquisition intervals do not land on exactly 1 s.
        for i in range(1,335):
            now=100.002+i*.003
            output.received_monotonic_s=now;output.timestamp=1234.002+i*.003
            kwargs=({'mode':'entry','entry_time_s':i*.003} if i<334
                    else {'mode':'path','path_time_s':i*.003-1.})
            provider.command(output=output,sensor=replace(sensor,observed_at_s=now),
                monotonic_s=now,actual_dt_s=.003,internal_setpoint_n=5.,**kwargs)
        assert runtime.controller.last_path_time_s==pytest.approx(.002)
        assert provider.last_result['formal_time_s']==pytest.approx(.002)


def test_mature_qualification_entry_and_formal_clock_use_same_native_provider(lib,monkeypatch):
    from test_contact_qualification_provider import _control,_successful_baseline
    from step5d_autotune_v4_r004 import baseline_runtime
    runtime,provider,output,sensor=setup(lib)
    with runtime:
        # Existing baseline provider state is carried into the state-25 seam.
        call(provider,output,sensor,0)
        control=_control(provider)
        from step6_figure8_autotune_v1.live_composition import figure8_motion_profile
        control.motion_profile=figure8_motion_profile()
        control._contract.model_hashes=dict(provider.model_hashes)
        control._last_monotonic_s=100.
        control._origin_monotonic_s=100.
        control._path_origin_monotonic_s=None
        output.integer_echoes={26:25}
        output.stationary=True
        monkeypatch.setattr(baseline_runtime,'step_baseline',_successful_baseline)
        phases=[]
        for tick in range(1,31917):
            now=100.+tick*.002
            output.received_monotonic_s=now;output.timestamp=1234.+tick*.002
            try:
                command=control.step(output=output,sensor=replace(sensor,observed_at_s=now),
                    monotonic_s=now,command_sequence=tick)
            except Exception as error:
                raise AssertionError({"tick":tick,"twist":provider.last_result.get("applied_twist_base"),
                    "profile":control.motion_profile}) from error
            phases.append(provider.last_result['phase'])
            assert int(command.command_mode)==2  # same TP PATH transport mode
            assert command.qdot==provider.last_result['qdot_rad_s']
        assert phases[:500]==['entry']*500
        assert phases[500:]==['path']*31416
        assert provider.last_result['execution_time_s']==pytest.approx(63.830)
        assert provider.last_result['formal_time_s']==pytest.approx(62.830)


def test_outer_gate_failure_rolls_back_native_provider_transaction(lib,monkeypatch):
    from test_contact_qualification_provider import _control,_successful_baseline
    from step5d_autotune_v4_r004 import baseline_runtime
    from step5d_autotune_v4_r004.qualification import QualificationControlError
    runtime,provider,output,sensor=setup(lib)
    with runtime:
        call(provider,output,sensor,0)
        before=provider.snapshot()
        control=_control(provider)
        # Deliberately mismatched binding exercises a post-provider failure.
        control._contract.model_hashes={'calibration':'wrong'}
        control._last_monotonic_s=100.;control._origin_monotonic_s=100.
        control._path_origin_monotonic_s=None
        output.integer_echoes={26:25};output.stationary=True
        output.received_monotonic_s=100.002;output.timestamp=1234.002
        monkeypatch.setattr(baseline_runtime,'step_baseline',_successful_baseline)
        with pytest.raises(QualificationControlError,match='hash binding differs'):
            control.step(output=output,sensor=replace(sensor,observed_at_s=100.002),
                monotonic_s=100.002,command_sequence=1)
        assert provider.snapshot()==before
