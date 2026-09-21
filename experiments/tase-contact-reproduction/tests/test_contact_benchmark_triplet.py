from pathlib import Path
import sys,json,gzip,xml.etree.ElementTree as ET
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from build_contact_benchmark_triplet import transform,SOURCE,BASENAME,build
from contact_yield_task_frame import FIGURE8_CONTACT_HOME_XYZ_M


def receipt():
    return {'user_home_confirmed':True,'home_pose':[.487834547,.129337053,.033,2.033134243,2.394988424,0],
            'home_q':[.61,-1.83,-2.53,-.35,1.57,3.59],
            'rtde':{'actual_TCP_pose':[.474,.17,.05,2.03,2.4,0],'actual_q':[.7,-1.79,-2.53,-.39,1.57,3.68],
                    'actual_TCP_speed':[0]*6,'tcp_offset':[0,0,.0874,0,0,0],'payload':.413,'payload_cog':[.0011,.0031,.0163]}}


def test_triplet_preserves_home_xyz_and_has_narrow_common_caps(tmp_path):
    p=tmp_path/'home.json';p.write_text(json.dumps(receipt()));out=tmp_path/'package';r=build(p,out)
    s=(out/f'{BASENAME}.script').read_text();assert '62.831853071796' in s
    assert 'codex_r006_finite(qdot, 0.050000000)' in s
    assert 'def codex_r006_actual_qd_ok(limit):' in s
    assert 'elif not codex_r006_actual_qd_ok(0.060000000):' in s
    assert 'return 76' in s
    assert 'local runtime_revision = 23' in s
    assert r['numeric_sanity']['tp_actual_joint_speed_guard_rad_s'] == pytest.approx(.06)
    assert 'local force_fuse_n = 20.000000000' in s
    assert 'travel >= 0.015000000' in s
    assert 'STEP5D_R008_WAVE4_FAR_NEAR_NO_ADMITTANCE_V1' in s
    assert 'local d_near_start_travel_m = 0.011029311' in s
    assert 'local v_far_m_s = 0.005000000' in s
    assert 'local v_near_m_s = 0.000500000' in s
    assert r['contact_search']['admittance_enabled'] is False
    assert r['contact_search']['far_speed_m_s'] == pytest.approx(0.005)
    assert r['contact_search']['near_speed_m_s'] == pytest.approx(0.0005)
    assert 'read_input_integer_register(36) < 1 or read_input_integer_register(25) != 2' in s
    assert 'read_input_integer_register(25) != 2 or read_input_integer_register(36) < 1' in s
    assert 'read_input_integer_register(36) < 3 or read_input_integer_register(25) != 2' not in s
    assert 'read_input_integer_register(25) != 2 or read_input_integer_register(36) < 3' not in s
    assert 'p[0.487834547000, 0.129337053000, 0.033000000000' in s
    assert 'pose_trans(pose_inv(expected), actual)' in s
    xml=ET.fromstring(gzip.decompress((out/f'{BASENAME}.urp').read_bytes()))
    cache=next(n.text for n in xml.iter() if n.tag.endswith('cachedContents'))
    assert cache==s
    assert r['numeric_sanity']['full_search_travel_time_s']<r['numeric_sanity']['search_timeout_s']


def test_tool_change_or_unconfirmed_home_fails_closed():
    h=receipt();h['rtde']['payload']=1.56
    with pytest.raises(ValueError,match='mass'):transform(SOURCE.read_text(),h,'2026-09-18T0000Z_TEST')
    h=receipt();h['user_home_confirmed']=False
    with pytest.raises(ValueError,match='Home receipt'):transform(SOURCE.read_text(),h,'2026-09-18T0000Z_TEST')


def test_home_package_preserves_xyz_and_bounds_initial_pose(tmp_path):
    from build_contact_home import build as build_home, BASENAME as home_name
    h=receipt();h['home_pose'][:3]=list(FIGURE8_CONTACT_HOME_XYZ_M)
    p=tmp_path/'home.json';p.write_text(json.dumps(h));out=tmp_path/'home-package'
    result=build_home(p,out);s=(out/f'{home_name}.script').read_text()
    assert result['home_pose'][:3]==list(FIGURE8_CONTACT_HOME_XYZ_M)
    assert 'initial_xyz_error > 0.002' in s
    assert 'movel(rise_pose, a=0.060, v=0.040, r=0.0)' in s
    assert 'movel(transfer_pose, a=0.135, v=0.090, r=0.0)' in s
    assert 'set_tcp(' not in s and 'zero_ftsensor(' not in s
    assert 'local delta_pose = pose_trans(pose_inv(target_pose), actual_pose)' in s


def test_generated_stop_branch_consumes_retained_command_once():
    import textwrap
    script=transform(SOURCE.read_text(),receipt(),'2026-09-20T1010Z_TEST')
    start=script.index('    if session_command == 3 and session_sequence > consumed_session_sequence:')
    end=script.index('    elif ',start)
    # Execute this generated arithmetic/branch subset with stopl as a recorder.
    # This checks retained register semantics, not UR runtime qualification.
    subset=textwrap.dedent('\n'.join(line for line in script[start:end].splitlines()
                                    if line.strip()!='end'))
    stops=[]; homes=[]
    state=dict(session_command=3,session_sequence=1,consumed_session_sequence=0,
               state=78,input_epoch=1,stopl=stops.append,
               locked_home_q_valid=False,locked_home_q=[0.]*6,
               get_actual_joint_positions=lambda:[0.]*6,
               fixed_home_pose=[0.]*6,current_ordinal=1,current_token=1,
               current_kind=1,runtime_revision=20,runtime_extension=618001,
               codex_r006_attempt_guard=0,codex_r006_attempt_reason=0,
               codex_r006_recover_home_after_fault=lambda *args: homes.append(args) or True)
    for _ in range(5):exec(subset,state)
    assert stops==[.25] and len(homes)==1
    assert state['consumed_session_sequence']==1
    assert state['state']==90 and state['reason']==4
    state['session_sequence']=2
    exec(subset,state)
    assert stops==[.25] and state['consumed_session_sequence']==2 and len(homes)==1
    assert 'elif state != 90 and not session_active' in script


def test_generated_stop_keeps_first_terminal_fault():
    import textwrap
    script=transform(SOURCE.read_text(),receipt(),'2026-09-20T1010Z_TEST')
    start=script.index('    if session_command == 3 and session_sequence > consumed_session_sequence:')
    end=script.index('    elif ',start)
    subset=textwrap.dedent('\n'.join(line for line in script[start:end].splitlines()
                                    if line.strip()!='end'))
    stops=[]
    state=dict(session_command=3,session_sequence=5,consumed_session_sequence=4,
               state=90,reason=43,return_guard=123,input_epoch=1,stopl=stops.append)
    exec(subset,state)
    assert not stops and state['reason']==43 and state['return_guard']==123
    assert state['consumed_session_sequence']==5


def test_every_generated_terminal_fault_attempts_home():
    script = transform(SOURCE.read_text(), receipt(), '2026-09-20T1010Z_TEST')

    # The definition plus one call from each terminal branch (STOP, wire,
    # invalid ARM, bad Home entry and execute failure) must remain present.
    assert script.count('codex_r006_recover_home_after_fault(') == 7
    assert 'elif integer_reason != 0 and session_command != 0 and state != 90:' in script
    assert 'if not codex_r006_entry_home_verified' in script
    assert 'if not codex_r006_execute_attempt' in script
    assert '# HOME_ENTRY_FAILURE: 69 ATTEMPT_ENTRY_NOT_CAPTURED_HOME; attempt bounded Home recovery' in script
    assert 'stop without Script2 auto-home' not in script
    recovery = script.split('def codex_r006_recovery_packet_guard', 1)[1].split(
        'def codex_r006_recovery_stationary', 1
    )[0]
    assert 'read_input_float_register(28) > 0.5' not in recovery
