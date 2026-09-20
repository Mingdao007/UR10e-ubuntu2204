#!/usr/bin/env python3
"""Bind the existing three-segment Home helper to preserved XYZ/new attitude."""
from pathlib import Path
import json,datetime,re,argparse
import numpy as np
from build_step4e_p0p1_programs import build_urp
from step5d_autotune_v4_r012.controller_triplet import validate_urscript_block_balance
from build_contact_benchmark_triplet import ROOT,CONTROLLER_DIR,transform,SOURCE

BASENAME='step5d_contact_home_v1'
HOME_SOURCE=ROOT/'programs/step5/step5d/step5d_autotune_start_hover_r001.script'


def recovery_geometry(home):
    """A small return to the already approved Home, bound to an observed start."""
    if home.get('bounded_recovery') is not True:return None
    from contact_yield_math import so3_exp,so3_log
    start=np.asarray(home['rtde']['actual_TCP_pose'],dtype=float)
    target=np.asarray(home['home_pose'],dtype=float)
    if start.shape!=(6,) or target.shape!=(6,) or not np.isfinite(start).all() or not np.isfinite(target).all():
        raise ValueError('recovery poses are invalid')
    turn=so3_log(so3_exp(target[3:])@so3_exp(start[3:]).T)
    if np.linalg.norm(target[:3]-start[:3])>.003 or np.linalg.norm(turn)>.020:
        raise ValueError('bounded Home recovery exceeds 3mm or 20mrad')
    if start[2]<target[2]-.001:raise ValueError('recovery starts below existing Home floor')
    return start,target,turn


def withdrawal_geometry(home):
    if home.get('bounded_withdrawal') is not True:return None
    from contact_yield_math import so3_exp,so3_log
    start=np.asarray(home['rtde']['actual_TCP_pose'],dtype=float);target=np.asarray(home['home_pose'],dtype=float)
    if start.shape!=(6,) or target.shape!=(6,) or not np.isfinite(start).all() or not np.isfinite(target).all():
        raise ValueError('withdrawal poses are invalid')
    if (np.linalg.norm(start[:2]-target[:2])>.0005 or not 0<=target[2]-start[2]<=.015
        or np.linalg.norm(so3_log(so3_exp(target[3:])@so3_exp(start[3:]).T))>.010):
        raise ValueError('withdrawal exceeds the existing vertical search envelope')
    return start,target


def build(receipt,output):
    home=json.loads(receipt.read_text());stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H%MZ_CONTACT_HOME_V1')
    # Reuse the same tool/Home validation as the paired resident.
    transform(SOURCE.read_text(),home,stamp)
    target=np.asarray(home['home_pose']);observed=np.asarray(home['rtde']['actual_TCP_pose'])
    recovery=recovery_geometry(home)
    withdrawal=withdrawal_geometry(home)
    if not np.allclose(target[:3],[.487834547,.129337053,.033],atol=1e-12):raise ValueError('original Home XYZ changed')
    if np.linalg.norm(target[:3]-observed[:3])>.08:raise ValueError('Home transfer exceeds 80mm bound')
    text=HOME_SOURCE.read_text().replace('step5d_autotune_start_hover_r001',BASENAME)
    text=re.sub(r'^# VERSION: .*$',f'# VERSION: {stamp}',text,flags=re.M)
    pose='p['+', '.join(f'{x:.12f}' for x in target)+']'
    text=re.sub(r'local target_pose = p\[[^\]]+\]',f'local target_pose = {pose}',text)
    text=re.sub(r'^# TARGET_POSE: .*$',f'# TARGET_POSE: {pose}',text,flags=re.M)
    # Slower than inherited helper, identical geometric three-segment route.
    text=text.replace('a=0.060, v=0.040','a=0.050, v=0.010').replace('a=0.135, v=0.090','a=0.050, v=0.010')
    for a,b in [('= 0.060','= 0.050'),('= 0.135','= 0.050'),('= 0.040','= 0.010'),('= 0.090','= 0.010')]:text=text.replace(a,b)
    if recovery is not None or withdrawal is not None:
        text=text.replace('a=0.050, v=0.010','a=0.010, v=0.002')
        first,rest=text.split('\n',1)
        description='vertical withdrawal within the 15mm search envelope' if withdrawal is not None else 'translation<=3mm turn<=20mrad'
        text=first+'\n# BOUNDED_RECOVERY: observed start to approved Home; '+description+'; a=0.01 v=0.002\n'+rest
    marker='  local safe_transfer_z = 0.033000000\n'
    initial='p['+', '.join(f'{x:.12f}' for x in observed)+']'
    guard=f'''  # Bind the initial pose to the fresh stationary read; reject an intervening move.
  local expected_initial = {initial}
  local initial_delta = pose_trans(pose_inv(expected_initial), current_pose)
  local initial_xyz_error = sqrt(initial_delta[0]*initial_delta[0] + initial_delta[1]*initial_delta[1] + initial_delta[2]*initial_delta[2])
  local initial_angle_error = sqrt(initial_delta[3]*initial_delta[3] + initial_delta[4]*initial_delta[4] + initial_delta[5]*initial_delta[5])
  if initial_xyz_error > 0.002 or initial_angle_error > 0.010:
    textmsg("contact_home: initial pose changed; no motion")
    halt
  end
'''
    if text.count(marker)!=1:raise ValueError('Home helper source differs')
    text=text.replace(marker,guard+marker)
    text='\n'.join(l for l in text.splitlines() if not l.startswith(('# MOTION_SEGMENT_', '# GEOMETRY_BASIS_')))+'\n'
    text=text.replace('# BLEND_RADIUS_M:', '# MOTION: preserve height for XY transfer, then descend to original Home; a=0.05m/s2 v=0.01m/s\n# BLEND_RADIUS_M:')
    if recovery is not None or withdrawal is not None:text=text.replace('a=0.05m/s2 v=0.01m/s','a=0.01m/s2 v=0.002m/s')
    validate_urscript_block_balance(text)
    for forbidden in ('zero_ftsensor(', 'set_tcp(', 'set_payload(', 'speedj(', 'read_input_'):
        if forbidden in text:raise ValueError('forbidden Home helper side effect')
    output.mkdir(parents=True,exist_ok=True)
    for suffix,data in [('script',text.encode()),('txt',f'Contact Home\n{stamp}\n{BASENAME}\n'.encode()),('urp',build_urp(text,BASENAME,CONTROLLER_DIR))]:
        (output/f'{BASENAME}.{suffix}').write_bytes(data)
    info={'basename':BASENAME,'stamp':stamp,'controller_directory':CONTROLLER_DIR,'home_pose':target.tolist(),
          'bounded_recovery':recovery is not None,
          'bounded_withdrawal':withdrawal is not None,
          'numeric_sanity':{'max_transfer_distance_m':float(np.linalg.norm(target[:3]-observed[:3])),
                            'speed_m_s':.002 if recovery is not None or withdrawal is not None else .01,'acceleration_m_s2':.01 if recovery is not None or withdrawal is not None else .05,'initial_position_tolerance_m':.002,
                            'contact':withdrawal is not None,'force_control':False,'joint_path_check':'required separately'},'live_executed':False}
    (output/f'{BASENAME}.binding.json').write_text(json.dumps(info,indent=2)+'\n');return info

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--home-receipt',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(json.dumps(build(a.home_receipt,a.output)))
