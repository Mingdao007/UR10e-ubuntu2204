#!/usr/bin/env python3
"""Generate an isolated contact-QP resident, never Load/Play or open a device."""
from pathlib import Path
import argparse,datetime,hashlib,json,re
import numpy as np
from build_step4e_p0p1_programs import build_urp
from step5d_autotune_v4_r012.controller_triplet import validate_urscript_block_balance
from contact_benchmark_protocol import Task

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'programs/step5/step5d/step5d_strict_rnn_autotune_v4_r012.script'
BASENAME='step5d_contact_six_qp_v1'
CONTROLLER_DIR='/programs/andyl/kunwei/step5'
PROTOCOL=618001
REVISION=19


def transform(source,home,stamp):
    if home.get('user_home_confirmed') is not True:raise ValueError('user-defined Home receipt required')
    obs=home['rtde'];pose=np.asarray(home['home_pose']);q=np.asarray(home['home_q'])
    if pose.shape!=(6,) or q.shape!=(6,) or not np.isfinite(pose).all() or not np.isfinite(q).all():raise ValueError('invalid Home')
    if np.linalg.norm(obs['actual_TCP_speed'])>.00001:raise ValueError('Home not stationary')
    if not np.allclose(obs['tcp_offset'],[0,0,.0874,0,0,0],atol=1e-9):raise ValueError('TCP differs')
    if not np.isclose(obs['payload'],.413,atol=1e-6) or not np.allclose(obs['payload_cog'],[.0011,.0031,.0163],atol=1e-6):raise ValueError('tool mass/CoG differs')
    if 'while path_elapsed_s < 60.000000000 and not r012_path_early_end:' not in source:raise ValueError('source resident differs')
    body=source.replace('step5d_strict_rnn_autotune_v4_r012',BASENAME)
    body=re.sub(r'^# VERSION: .*$',f'# VERSION: {stamp}',body,flags=re.M)
    body=body.replace('612012',str(PROTOCOL))
    body=re.sub(r'local runtime_revision = [-0-9]+',f'local runtime_revision = {REVISION}',body)
    body=body.replace('write_output_integer_register(33, 12)',f'write_output_integer_register(33, {REVISION})')
    home_text='p['+', '.join(f'{v:.12f}' for v in pose)+']'
    body=re.sub(r'local fixed_home_pose = p\[[^\]]+\]',f'local fixed_home_pose = {home_text}',body)
    body=re.sub(r'^# FIXED_HOME_POSE: .*$',f'# FIXED_HOME_POSE: {home_text}',body,flags=re.M)
    body=body.replace('while path_elapsed_s < 60.000000000 and',f'while path_elapsed_s < {Task().duration_s:.12f} and')
    # Narrow inherited speed bounds; leave register layout and fault/return lifecycle intact.
    body=body.replace('codex_r006_finite(qdot, 5.000000000)','codex_r006_finite(qdot, 0.050000000)')
    body=body.replace('speedj(baseline_qdot, 40.000000000,','speedj(baseline_qdot, 5.000000000,')
    body=body.replace('speedj(path_qdot, 40.000000000,','speedj(path_qdot, 5.000000000,')
    body=body.replace('local d_near_start_travel_m = 0.011029311','local d_near_start_travel_m = 0.000000000')
    body=body.replace('local v_far_m_s = 0.005000000','local v_far_m_s = 0.000200000')
    body=body.replace('local v_near_m_s = 0.000500000','local v_near_m_s = 0.000200000')
    body=body.replace('local force_fuse_n = 50.000000000','local force_fuse_n = 20.000000000')
    body=body.replace('travel >= 0.025000000','travel >= 0.015000000')
    body=body.replace('packet_reason, 60.0, 100.0, 3.0','packet_reason, 20.0, 20.0, 2.0')
    body=body.replace('packet_reason, 100.0, 100.0, 3.0','packet_reason, 20.0, 20.0, 2.0')
    # Equivalent +/-pi rotation vectors must not falsely reject the taught Home.
    old_rotation = '  local rotation_error = sqrt((actual[3] - expected[3]) * (actual[3] - expected[3]) + (actual[4] - expected[4]) * (actual[4] - expected[4]) + (actual[5] - expected[5]) * (actual[5] - expected[5]))'
    if body.count(old_rotation) != 1:raise ValueError('Home orientation gate source differs')
    body=body.replace(old_rotation,'  local relative_pose = pose_trans(pose_inv(expected), actual)\n  local rotation_error = sqrt(relative_pose[3]*relative_pose[3] + relative_pose[4]*relative_pose[4] + relative_pose[5]*relative_pose[5])')
    # RTDE holds its last input image. Consume STOP once and latch the terminal
    # resident state until a new Play; repeated packets must not call stopl.
    stop_start = '    if session_command == 3 and session_sequence > consumed_session_sequence:\n'
    stop_end = '    elif integer_reason != 0 and session_command != 0:\n'
    if body.count(stop_start) != 1 or body.count(stop_end) != 1:
        raise ValueError('resident STOP branch source differs')
    begin=body.index(stop_start);end=body.index(stop_end,begin)
    old_body=body[begin+len(stop_start):end]
    body=body[:begin]+stop_start+'      consumed_session_sequence = session_sequence\n      if state != 90:\n'+''.join('  '+line+'\n' for line in old_body.splitlines())+'      end\n'+body[end:]
    body=body.replace(stop_end,'    elif integer_reason != 0 and session_command != 0 and state != 90:\n')
    arm='    elif not session_active and not completed and session_command == 1 and '
    if body.count(arm)!=1:raise ValueError('resident ARM branch source differs')
    body=body.replace(arm,'    elif state != 90 and not session_active and not completed and session_command == 1 and ')
    lines=[line for line in body.splitlines() if not line.startswith(('# R006_ACTIVE_CAPS:','# ROLE:','# CONTACT_SEARCH:'))]
    lines[1:1]=[f'# ROLE: six-law shared-QP preparation; requires matching host owner {PROTOCOL}',
        '# CONTACT_CAPS: qdot<=0.05rad/s; speedj_accel=5rad/s2; force_norm<20N; torque_norm<2Nm',
        '# CONTACT_SEARCH: constant 0.0002m/s; travel<=0.015m; timeout=90s; original Home XYZ with current aligned attitude',
        '# EOAT: payload=0.413kg CoG=[0.0011,0.0031,0.0163]m TCP=[0,0,0.0874,0,0,0]',
        '# HOST_BACKEND: native-six-law + osqp-codegen-c; legacy RNN qualification is not reusable']
    body='\n'.join(lines)+'\n'
    validate_urscript_block_balance(body)
    for forbidden in ('set_tcp(', 'set_payload(', 'zero_ftsensor(', 'speedj(path_qdot, 40.'):
        if forbidden in body:raise ValueError(f'forbidden package effect: {forbidden}')
    return body


def build(home_path,output):
    home=json.loads(home_path.read_text());stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H%MZ_CONTACT_SIX_QP_V1_618001')
    source=SOURCE.read_text();script=transform(source,home,stamp);output.mkdir(parents=True,exist_ok=True)
    txt=f'Contact six-law shared QP\nPROGRAM={BASENAME}\nVERSION={stamp}\nPROTOCOL={PROTOCOL}\nLIVE_QUALIFIED=false\n'
    for suffix,data in [('script',script.encode()),('txt',txt.encode()),('urp',build_urp(script,BASENAME,CONTROLLER_DIR))]:
        (output/f'{BASENAME}.{suffix}').write_bytes(data)
    manifest={'schema':'contact-qp-package-preparation-v1','basename':BASENAME,'stamp':stamp,'controller_directory':CONTROLLER_DIR,
              'revision':REVISION,'protocol':PROTOCOL,'home_receipt_sha256':hashlib.sha256(home_path.read_bytes()).hexdigest(),
              'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'live_qualified':False,
              'geometry':Task().sanity(),'source_owner':'R012 lifecycle; narrowed common caps and new Home/identity/full-period duration'}
    manifest['numeric_sanity']={'duration_s':Task().duration_s,'span_m':[.08,.02],
        'peak_nominal_linear_speed_bound_m_s':Task().sanity()['speed_upper_bound_m_s'],
        'host_tangent_speed_cap_m_s':.01,'tp_joint_speed_limit_rad_s':.05,
        'tp_speedj_acceleration_rad_s2':5.,'search_speed_m_s':.0002,
        'search_travel_limit_m':.015,'search_timeout_s':90.,'full_search_travel_time_s':75.,
        'raw_force_limit_n':20.,'raw_torque_limit_nm':2.,'fixed_orientation_approach_axis':[0,0,-1],
        'home_xyz_m':home['home_pose'][:3],
        'scope':'local package arithmetic; workspace clearance and owner dispatch pending'}
    (output/f'{BASENAME}.binding.json').write_text(json.dumps(manifest,indent=2)+'\n');return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--home-receipt',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(json.dumps(build(a.home_receipt,a.output),indent=2))
