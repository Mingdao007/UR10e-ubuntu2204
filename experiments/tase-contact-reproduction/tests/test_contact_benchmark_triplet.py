from pathlib import Path
import sys,json,gzip,re,xml.etree.ElementTree as ET
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from build_contact_benchmark_triplet import transform,SOURCE,BASENAME,build
from contact_yield_task_frame import FIGURE8_CONTACT_HOME_XYZ_M
from contact_eoat_installation_binding import (
    InstallationBindingError,
    validate_urp_binding,
)
from build_step4e_p0p1_programs import build_urp
from figure8_home_config import CANONICAL_FIGURE8_HOME_POSE, CANONICAL_FIGURE8_HOME_Q
from contact_yield_live_contract import (
    YieldLiveContractError,
    validate_contact_package_home_identity,
)


def receipt():
    pose=list(CANONICAL_FIGURE8_HOME_POSE)
    q=list(CANONICAL_FIGURE8_HOME_Q)
    actual_pose=list(pose);actual_pose[0]+=0.0001
    actual_q=list(q);actual_q[0]+=0.0001
    return {'user_home_confirmed':True,'home_pose':pose,'home_q':q,
            'rtde':{'actual_TCP_pose':actual_pose,'actual_q':actual_q,
                    'actual_TCP_speed':[0]*6,'actual_qd':[0]*6,'tcp_offset':[0,0,.0874,0,0,0],
                    'payload':.413,'payload_cog':[.0011,.0031,.0163],
                    'safety_mode':1,'robot_mode':7,'runtime_state':1}}


def test_triplet_uses_approved_home_and_has_narrow_common_caps(tmp_path):
    p=tmp_path/'home.json';p.write_text(json.dumps(receipt()));out=tmp_path/'package';r=build(p,out)
    s=(out/f'{BASENAME}.script').read_text();assert '63.881853071796' in s
    assert 'codex_r006_echo(epoch, ordinal, 40, token, 0, consumed, kind, 0' in s
    assert 'codex_r006_finite(qdot, 0.050000000)' in s
    assert 'def codex_r006_actual_qd_ok(limit):' in s
    assert 'def codex_r006_cached_qdot(index):' in s
    assert 'local baseline_qdot = [codex_r006_cache_d37' in s
    assert 'local path_qdot = [codex_r006_cache_d37' in s
    assert 'elif codex_r006_cache_d27 < 0.5' in s
    assert 'elif not codex_r006_actual_qd_ok(0.060000000):' in s
    assert 'return 76' in s
    assert (
        '  stopl(0.010000000)\n'
        '  stopj(20.000000000)\n'
        '  if not codex_r006_stationary(0.250000000):'
    ) in s
    fault = s[s.index('def codex_r006_fault'):s.index('def codex_r006_return_home')]
    assert fault.count('stopj(20.000000000)') == 2
    assert 'local runtime_revision = 31' in s
    stop_home=s.split('def codex_r006_stop_home_stationary_for_stop',1)[1].split('def codex_r006_echo',1)[0]
    assert 'codex_r006_home_close(home_pose, home_q)' in stop_home
    assert 'codex_r006_actual_qd_ok(0.060000000)' in stop_home
    assert 'linear > 0.000500000 or angular > 0.005000000' in stop_home
    assert 'codex_r006_packet_observe' not in stop_home
    stop=s.split('if session_command == 3 and session_sequence > consumed_session_sequence:',1)[1].split(
        'elif integer_reason != 0 and session_command != 0 and state != 90:',1)[0]
    assert 'state == 78 and return_guard == 123' in stop
    assert 'codex_r006_stop_home_stationary_for_stop' in stop
    assert 'def codex_r006_path_poststop_stationary' not in s
    release=s.split('def codex_r006_vertical_release_ready',1)[1].split('def codex_r006_return_home',1)[0]
    assert 'local transverse = sqrt(speed[0] * speed[0] + speed[1] * speed[1])' in release
    assert 'local vertical = codex_r006_abs(speed[2])' in release
    assert 'transverse > 0.000500000 or vertical > 0.001500000 or angular > 0.005000000' in release
    assert 'codex_r006_recovery_packet_guard(packet_reason, 20.0, 20.0, 2.0)' in release
    assert 'elapsed_s < required_s * 5.0' in release
    return_body=s.split('def codex_r006_return_home',1)[1].split('def codex_r006_execute_attempt',1)[0]
    assert return_body.index('codex_r006_vertical_release_ready') < return_body.index('movel(vertical_pose')
    after_lift=return_body.split('movel(vertical_pose',1)[1]
    assert 'if not codex_r006_recovery_stationary(0.250000000)' in after_lift
    assert after_lift.index('codex_r006_recovery_stationary(0.250000000)') < after_lift.index('movej(home_q')
    stationary=s.split('def codex_r006_recovery_stationary',1)[1].split('def codex_r006_pose_close',1)[0]
    assert 'linear > 0.000500000 or angular > 0.005000000' in stationary
    path_tail=s[s.index('codex_r006_echo(epoch, ordinal, 40, token, 0, consumed, kind, 0'):]
    assert path_tail.index('codex_r006_echo(epoch, ordinal, 40, token, 0') < path_tail.index('stopj(20.000000000)')
    assert path_tail.index('stopj(20.000000000)') < path_tail.index('codex_r006_return_home(')
    peak_speed=2*3.141592653589793*20.0*0.0001
    assert peak_speed > 0.0015  # 0.1 mm at 20 Hz must fail the vertical release bound.
    assert 'movej(home_q, a=0.050000000, v=0.050000000, t=0.0, r=0.0)' in s
    assert 'movel(transfer_pose, a=0.135, v=0.090, r=0.0)' not in s
    assert r['numeric_sanity']['tp_actual_joint_speed_guard_rad_s'] == pytest.approx(.06)
    assert 'local force_fuse_n = 20.000000000' in s
    assert 'travel >= 0.015000000' in s
    assert 'STEP5D_R008_WAVE4_FAR_NEAR_EARLY_BRAKE_008MM_V1' in s
    assert 'local d_near_start_travel_m = 0.008000000' in s
    assert 'local f_far_n = 0.300000000' in s
    assert 'local v_far_m_s = 0.005000000' in s
    assert 'local v_near_m_s = 0.000500000' in s
    assert 'local far_accel_m_s2 = 0.010000000' in s
    assert 'local near_accel_m_s2 = 0.005000000' in s
    assert 'if normal_force >= 0.500000000 or force_norm >= 0.700000000:' in s
    assert r['contact_search']['admittance_enabled'] is False
    assert r['contact_search']['source'] == 'config/tase_figure8_contact_search_rate460_early_brake_008mm_v1.json'
    assert r['contact_search']['figure8_protocol_id'] == 'figure8_window60_r013_rate460_v1'
    assert len(r['contact_search']['profile_sha256']) == 64
    assert r['contact_search']['far_speed_m_s'] == pytest.approx(0.005)
    assert r['contact_search']['near_speed_m_s'] == pytest.approx(0.0005)
    assert r['contact_search']['near_start_travel_m'] == pytest.approx(0.008)
    assert r['contact_search']['force_based_early_switch_n'] == pytest.approx(0.3)
    assert r['contact_search']['force_fuse_n'] == pytest.approx(20.0)
    assert r['contact_search']['max_travel_m'] == pytest.approx(0.015)
    assert '8 mm as an offline early-braking candidate' in r['contact_search']['current_native_adaptation']
    assert 'read_input_integer_register(36) < 1 or read_input_integer_register(25) != 2' in s
    assert 'read_input_integer_register(25) != 2 or read_input_integer_register(36) < 1' in s
    assert 'read_input_integer_register(36) < 3 or read_input_integer_register(25) != 2' not in s
    assert 'read_input_integer_register(25) != 2 or read_input_integer_register(36) < 3' not in s
    expected_home='p['+', '.join(f'{value:.12f}' for value in CANONICAL_FIGURE8_HOME_POSE)+']'
    assert f'local fixed_home_pose = {expected_home}' in s
    assert r['home_profile_id']=='step6.autotune/figure8-contact-derived-home-v1'
    assert r['home_pose_m_rad']==list(CANONICAL_FIGURE8_HOME_POSE)
    assert r['home_q_rad']==list(CANONICAL_FIGURE8_HOME_Q)
    expected_q='['+', '.join(f'{value:.12f}' for value in CANONICAL_FIGURE8_HOME_Q)+']'
    assert f'local locked_home_q = {expected_q}' in s
    assert 'local locked_home_q_valid = True' in s
    assert 'get_actual_joint_positions()' not in s[s.index('def step5d_contact_six_qp_v1():'):]
    assert 'arm_home_q' not in s
    startup_identity=(
        '  local completed = False\n'
        '  write_output_integer_register(32, 606006)\n'
        '  write_output_integer_register(33, 31)\n'
        '  write_output_integer_register(34, 618001)\n'
        '  while True:\n'
    )
    assert startup_identity in s
    program_entry=s.index('def step5d_contact_six_qp_v1():')
    startup=s.index(startup_identity,program_entry)
    packet_observe=s.index('codex_r006_packet_observe()',startup)
    assert startup < packet_observe
    assert 'pose_trans(pose_inv(expected), actual)' in s
    xml=ET.fromstring(gzip.decompress((out/f'{BASENAME}.urp').read_bytes()))
    assert xml.attrib['installation']=='codex_eoat_probe_20260925_r2'
    assert xml.attrib['installationRelativePath']=='../../../codex_eoat_probe_20260925_r2'
    assert r['installation_binding']['controller_path']=='/programs/codex_eoat_probe_20260925_r2.installation'
    validate_urp_binding((out/f'{BASENAME}.urp').read_bytes(),basename=BASENAME,
                         controller_directory='/programs/andyl/kunwei/step5')
    cache=next(n.text for n in xml.iter() if n.tag.endswith('cachedContents'))
    assert cache==s
    validate_contact_package_home_identity(out)
    assert r['numeric_sanity']['full_search_travel_time_s']<r['numeric_sanity']['search_timeout_s']


def test_generated_triplet_uses_supported_abs_and_r31_identity(tmp_path):
    p=tmp_path/'home.json';p.write_text(json.dumps(receipt()))
    out=tmp_path/'package';manifest=build(p,out)
    script=(out/f'{BASENAME}.script').read_text()
    txt=(out/f'{BASENAME}.txt').read_text()
    release=script.split('def codex_r006_vertical_release_ready',1)[1].split('def codex_r006_return_home',1)[0]
    assert not re.search(r'(?<![A-Za-z0-9_])abs\s*\(',script)
    assert 'def codex_r006_abs(value):' in script
    assert 'local vertical = codex_r006_abs(speed[2])' in release
    assert manifest['revision']==31
    assert manifest['stamp'].endswith('_CONTACT_SIX_QP_V1_R31_618001')
    assert f'# VERSION: {manifest["stamp"]}' in script
    assert f'VERSION={manifest["stamp"]}' in txt
    assert 'local runtime_revision = 31' in script
    assert 'write_output_integer_register(33, 31)' in script


def _write_package_script_and_cache(package_dir, script):
    script_path=package_dir/f'{BASENAME}.script'
    script_path.write_text(script)
    urp_path=package_dir/f'{BASENAME}.urp'
    xml=ET.fromstring(gzip.decompress(urp_path.read_bytes()))
    next(n for n in xml.iter() if n.tag.endswith('cachedContents')).text=script
    urp_path.write_bytes(gzip.compress(ET.tostring(xml,encoding='utf-8',xml_declaration=True),mtime=0))


def test_home_validator_rejects_changed_joint_target_even_when_cache_matches(tmp_path):
    p=tmp_path/'home.json';p.write_text(json.dumps(receipt()))
    out=tmp_path/'package';build(p,out)
    script=(out/f'{BASENAME}.script').read_text()
    expected='['+', '.join(f'{value:.12f}' for value in CANONICAL_FIGURE8_HOME_Q)+']'
    changed='['+', '.join(f'{value + (0.01 if index == 0 else 0.0):.12f}' for index,value in enumerate(CANONICAL_FIGURE8_HOME_Q))+']'
    _write_package_script_and_cache(out,script.replace(expected,changed))
    with pytest.raises(YieldLiveContractError,match='different joint Home'):
        validate_contact_package_home_identity(out)


def test_home_validator_rejects_stale_urp_cache(tmp_path):
    p=tmp_path/'home.json';p.write_text(json.dumps(receipt()))
    out=tmp_path/'package';build(p,out)
    urp_path=out/f'{BASENAME}.urp'
    xml=ET.fromstring(gzip.decompress(urp_path.read_bytes()))
    next(n for n in xml.iter() if n.tag.endswith('cachedContents')).text += '\n# stale cache'
    urp_path.write_bytes(gzip.compress(ET.tostring(xml,encoding='utf-8',xml_declaration=True),mtime=0))
    with pytest.raises(YieldLiveContractError,match='cache differs'):
        validate_contact_package_home_identity(out)


def test_home_validator_ignores_comments_but_rejects_duplicate_pose_assignment(tmp_path):
    p=tmp_path/'home.json';p.write_text(json.dumps(receipt()))
    out=tmp_path/'package';build(p,out)
    script=(out/f'{BASENAME}.script').read_text()
    commented=script+'\n# local fixed_home_pose = p[0, 0, 0, 0, 0, 0]\n'
    _write_package_script_and_cache(out,commented)
    validate_contact_package_home_identity(out)
    duplicated=commented+'\nlocal fixed_home_pose = p[0, 0, 0, 0, 0, 0]\n'
    _write_package_script_and_cache(out,duplicated)
    with pytest.raises(YieldLiveContractError,match='different Cartesian Home'):
        validate_contact_package_home_identity(out)


def test_generator_rejects_receipt_with_stale_home_identity():
    h=receipt()
    h['home_pose']=[.487834547,.129337053,.033,2.033134243,2.394988424,0]
    with pytest.raises(ValueError,match='approved Figure-eight Home'):
        transform(SOURCE.read_text(),h,'2026-09-25T0000Z_TEST')
    h=receipt();h['home_q'][0]+=.1
    with pytest.raises(ValueError,match='joint target differs from approved'):
        transform(SOURCE.read_text(),h,'2026-09-25T0000Z_TEST')


def test_generator_rejects_measured_state_outside_home():
    h=receipt();h['rtde']['actual_q'][0]+=.1
    with pytest.raises(ValueError,match='measured joints are not at approved'):
        transform(SOURCE.read_text(),h,'2026-09-25T0000Z_TEST')


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
    home_xml=ET.fromstring(gzip.decompress((out/f'{home_name}.urp').read_bytes()))
    assert home_xml.attrib['installation']=='codex_eoat_probe_20260925_r2'
    validate_urp_binding((out/f'{home_name}.urp').read_bytes(),basename=home_name,
                         controller_directory='/programs/andyl/kunwei/step5')
    assert result['installation_binding']['controller_path']=='/programs/codex_eoat_probe_20260925_r2.installation'
    assert result['home_pose'][:3]==list(FIGURE8_CONTACT_HOME_XYZ_M)
    assert 'initial_xyz_error > 0.002' in s
    assert 'movel(rise_pose, a=0.060, v=0.040, r=0.0)' in s
    assert 'movel(transfer_pose, a=0.135, v=0.090, r=0.0)' in s
    assert 'set_tcp(' not in s and 'zero_ftsensor(' not in s
    assert 'local delta_pose = pose_trans(pose_inv(target_pose), actual_pose)' in s


def test_wrong_default_installation_is_rejected_for_tase_package():
    unbound = build_urp(SOURCE.read_text(), BASENAME, '/programs/andyl/kunwei/step5')
    with pytest.raises(InstallationBindingError, match='installation differs'):
        validate_urp_binding(unbound, basename=BASENAME,
                             controller_directory='/programs/andyl/kunwei/step5')


def test_generated_stop_branch_consumes_retained_command_once():
    import textwrap
    script=transform(SOURCE.read_text(),receipt(),'2026-09-20T1010Z_TEST')
    start=script.index('    if session_command == 3 and session_sequence > consumed_session_sequence:')
    end=script.index('    elif ',start)
    # Execute this generated arithmetic/branch subset with stopl as a recorder.
    # This checks retained register semantics, not UR runtime qualification.
    subset=textwrap.dedent('\n'.join(line for line in script[start:end].splitlines()
                                    if line.strip()!='end'))
    stops=[]; homes=[]; home_checks=[]
    state=dict(session_command=3,session_sequence=1,consumed_session_sequence=0,
               state=78,return_guard=123,input_epoch=1,stopl=stops.append,
               locked_home_q_valid=True,locked_home_q=[0.]*6,
               fixed_home_pose=[0.]*6,current_ordinal=1,current_token=1,
               current_kind=1,runtime_revision=31,runtime_extension=618001,
               codex_r006_stop_home_stationary_for_stop=lambda *args: home_checks.append(args) or True,
               codex_r006_attempt_guard=0,codex_r006_attempt_reason=0,
               codex_r006_recover_home_after_fault=lambda *args: homes.append(args) or True)
    for _ in range(5):exec(subset,state)
    assert stops==[] and homes==[] and len(home_checks)==1
    assert state['consumed_session_sequence']==1
    assert state['state']==90 and state['reason']==4
    assert state['return_guard']==123
    state['session_sequence']=2
    exec(subset,state)
    assert stops==[] and state['consumed_session_sequence']==2 and homes==[]
    assert 'elif state != 90 and not session_active' in script


def test_generated_stop_outside_verified_home_keeps_recovery_route():
    import textwrap
    script=transform(SOURCE.read_text(),receipt(),'2026-09-20T1010Z_TEST')
    start=script.index('    if session_command == 3 and session_sequence > consumed_session_sequence:')
    end=script.index('    elif integer_reason != 0 and session_command != 0 and state != 90:',start)
    subset=textwrap.dedent('\n'.join(line for line in script[start:end].splitlines()
                                    if line.strip()!='end'))
    stops=[];homes=[]
    state=dict(session_command=3,session_sequence=5,consumed_session_sequence=4,
               state=25,return_guard=0,input_epoch=2,stopl=stops.append,
               locked_home_q_valid=True,locked_home_q=[0.]*6,
               fixed_home_pose=[0.]*6,current_ordinal=1,current_token=1,
               current_kind=1,runtime_revision=31,runtime_extension=618001,
               codex_r006_stop_home_stationary_for_stop=lambda *_args: pytest.fail('Home shortcut used away from state 78'),
               codex_r006_attempt_guard=0,codex_r006_attempt_reason=0,
               codex_r006_recover_home_after_fault=lambda *args: homes.append(args) or False)
    exec(subset,state)
    assert stops==[.25] and len(homes)==1
    assert state['state']==90


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
