"""Real sole-writer loop with deterministic packet echo doubles, no endpoints.

Native controller is tested separately; this fixture isolates publication,
consumed phase, RTDE clock, full-cycle fence, and evidence finalization.
"""
from types import SimpleNamespace
from dataclasses import replace
import numpy as np
import pytest
import step5d_autotune_v4_r004_live_writer as writer_module
from step5d_autotune_v4_r004.campaign import build_campaign_plan
from step5d_autotune_v4_r004.qualification import QualificationCommand
from step5d_autotune_v4_r004.wire import CommandMode, SessionCommand
from step5d_autotune_v4_r004.session import SessionPhase
from contact_yield_protocol import PERIOD_S, Task
from yield_contact_provider import YieldContactProvider
from test_step5d_autotune_v4_r004_live_boundary import _prerequisites
from test_contact_qualification_provider import _output, _sensor


def exercise_writer_loop(tmp_path,monkeypatch, *, entry_aware=True, cached_at_end=False,
                         home_rotation=(0., 0., 0.), terminal_rotation=None,
                         native_provider=False, writer_class=None):
    clock=SimpleNamespace(t=0.,ticks=0,ended=False,cache_injected=False,previous=None)
    transport=SimpleNamespace(send_packet=lambda *_args: None)
    provider=SimpleNamespace(execution_command=lambda **_kw:None,last_result=None,
        runtime=SimpleNamespace(anchor=np.zeros(3),basis=np.eye(3),controller=SimpleNamespace(task=Task())))
    if native_provider:
        # Preserve the route type while this fixture doubles command generation.
        # Numerical provider behavior is covered by the full-writer tests.
        native = object.__new__(YieldContactProvider)
        native.__dict__.update(vars(provider))
        provider = native
    if not entry_aware:
        del provider.execution_command
        def legacy_reference(_stage,_xy,t):
            ref=Task().reference(t)
            return {"desired_xy":ref["position_m"][:2],"desired_velocity_xy":ref["velocity_m_s"][:2],
                    "path_time_s":t,"phase_rad":.1*t}
        monkeypatch.setattr(writer_module,'step5_path_reference',legacy_reference)
    class Control:
        contact_command_provider=provider
        def __init__(self,*_a,**_kw):self.origin=None
        def step(self,*,output,sensor,monotonic_s,command_sequence):
            if output.integer_echoes[26]==25:
                if self.origin is None:self.origin=monotonic_s
                phase,t=(YieldContactProvider.execution_phase(monotonic_s-self.origin) if entry_aware
                         else ("path",monotonic_s-self.origin))
                provider.last_result={'phase':phase,'sample_time_s':monotonic_s,'qdot_rad_s':(0.,)*6,
                    'entry_time_s':t if phase=='entry' else None,'formal_time_s':t if phase=='path' else None}
                mode=CommandMode.PATH
            else:mode=CommandMode.BASELINE
            return QualificationCommand(mode,(0.,)*6,5.,5.,1,'fixture','')
    monkeypatch.setattr(writer_module,'CanonicalQualificationControl',Control)
    def sleep(seconds):clock.t+=seconds
    samples=[]
    writer_type=writer_class or writer_module.LiveR004Writer
    writer=writer_type(_prerequisites(),authority_root=tmp_path/'authority',
        route_id='r004-test-route',attempt_id='r004-yield-fixture',controller_transport=transport,
        kunwei_transport=object(),mono_clock=lambda:clock.t,wall_clock=lambda:100.,sleep=sleep,
        path_sample_sink=samples.append)
    # Admission is outside this unit's scope: no open/arm/device operation.
    attempt=build_campaign_plan(writer.contract)[3]
    writer.session=SimpleNamespace(phase=SessionPhase.RUNNING,finish_attempt=lambda _d:None)
    writer._ordinal=attempt.ordinal;writer._kind=attempt.kind;writer._candidate_token=17
    writer._baseline_successes=3;writer._session_command=SessionCommand.HOLD
    writer._home=SimpleNamespace(pose=(0., 0., 0., *home_rotation),q=(0.,)*6)
    writer._host_hard_tube=None
    writer._fail_closed=lambda _reason:None
    def end(_sequence):clock.ended=True;return True
    writer._r013_path_early_end_controller=SimpleNamespace(request_early_end=end,requested=False)
    def poll(**_kwargs):
        state=78 if clock.ended else 20 if clock.ticks==0 else 21 if clock.ticks==1 else 25
        if cached_at_end and state==25 and clock.t>=60.004 and not clock.cache_injected:
            clock.cache_injected=True
            writer._last_poll_was_fresh=False
            return clock.previous
        pose=(0.,)*6;speed=(0.,)*6
        sequence=writer._last_writer_sequence or 0
        if state==25 and writer._last_writer_sequence is not None:
            entry=writer._packet_history.consumed(sequence)
            if entry.reference_phase=='path' or (not entry_aware and provider.last_result is not None):
                t=entry.reference_time_s if entry_aware else provider.last_result['formal_time_s']
                ref=Task().reference(t)
                pose=(*ref['position_m'],0.,0.,0.);speed=(*ref['velocity_m_s'],0.,0.,0.)
        clock.ticks+=1
        if state == 78 and terminal_rotation is not None:
            pose = (*pose[:3], *terminal_rotation)
        writer._last_poll_was_fresh=True
        writer._last_rtde_frame_sequence=clock.t
        writer._last_rtde_frame_mono_s=clock.t
        result=replace(_output(state=state),timestamp=clock.t,observed_at_s=100.+clock.t,
            received_monotonic_s=clock.t,consumed_packet_sequence=sequence,
            tcp_pose_m_rad=pose,tcp_speed_m_s_rad_s=speed,
            integer_echoes={26:state,31:127})
        clock.previous=result
        return result
    def sensor():
        writer._last_kunwei_frame_sequence=clock.ticks
        writer._last_kunwei_observed_s=clock.t
        return replace(_sensor(),normal_load_n=5.,force_norm_n=5.,filtered_normal_n=5.,
            observed_at_s=clock.t,wrench=(0.,0.,-5.,0.,0.,0.))
    writer._poll_checked=poll;writer._read_sensor=sensor
    evidence=writer.execute_attempt(attempt,timeout_s=70.)
    return evidence, samples, clock


def test_real_writer_excludes_entry_echoes_and_keeps_complete_formal_period(tmp_path,monkeypatch):
    evidence,samples,clock=exercise_writer_loop(tmp_path,monkeypatch)
    assert evidence.path_duration_s>=PERIOD_S
    assert evidence.metrics['full_path_bin_count']==629
    assert evidence.metrics['full_force_mae_n']==0.
    assert len(samples)>31000
    assert samples[0].observed_at_s>=1.
    assert samples[-1].path_time_s>62.82
    assert 63.83<clock.t<63.85
