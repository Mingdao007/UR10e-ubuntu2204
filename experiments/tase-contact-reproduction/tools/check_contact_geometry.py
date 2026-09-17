#!/usr/bin/env python3
"""Offline fixed-attitude trajectory IK on an explicitly supplied RTDE snapshot."""
from pathlib import Path
import argparse,json,hashlib
import numpy as np
import pinocchio as pin
from contact_benchmark_protocol import Task
from step5c_calibrated_kinematics_audit import build_calibrated_model,base_to_tool0,rotvec_to_matrix
from step5d_autotune_v4_r004.calibrated_runtime import tcp_jacobian_base


def check(snapshot):
    task=Task(); b=build_calibrated_model(); obs=snapshot['rtde']
    pose=np.asarray(obs['actual_TCP_pose']);off=np.asarray(obs['tcp_offset']);q=np.asarray(obs['actual_q'])
    if not np.allclose(off,[0,0,.0874,0,0,0],atol=1e-10):raise ValueError('TCP offset differs')
    Rd=rotvec_to_matrix(pose[3:]);qs=[];conds=[];residuals=[]
    times=np.linspace(0,task.duration_s,629)
    for t in times:
        pd=pose[:3]+np.asarray(task.reference(t)['position_m'])
        for iteration in range(40):
            T=base_to_tool0(b,q);p=T.translation+T.rotation@off[:3]
            e=np.r_[pd-p,np.asarray(pin.log3(Rd@T.rotation.T))]
            if np.linalg.norm(e)<1e-9:break
            J=tcp_jacobian_base(b,q,off[:3]);q=q+np.linalg.solve(J,e)
        else:raise ValueError(f'IK failed at {t}')
        if np.any(q<b.model.lowerPositionLimit) or np.any(q>b.model.upperPositionLimit):raise ValueError('joint position limit')
        qs.append(q.copy());conds.append(float(np.linalg.cond(tcp_jacobian_base(b,q,off[:3]))));residuals.append(float(np.linalg.norm(e)))
    qs=np.array(qs);qd=np.gradient(qs,times,axis=0);qdd=np.gradient(qd,times,axis=0)
    maxqd=float(np.max(np.abs(qd)));maxqdd=float(np.max(np.abs(qdd)))
    return {'schema':'contact-offline-geometry-v1','claim_scope':'IK at observed height, not contact-plane clearance, collision checking or live qualification',
            'sample_count':len(times),'max_joint_speed_rad_s':maxqd,'max_joint_acceleration_rad_s2':maxqdd,
            'max_jacobian_condition':max(conds),'max_pose_residual':max(residuals),
            'qdot_limit_rad_s':.05,'qdot_check_pass':maxqd<.05,'joint_path_closed_rad':float(np.max(abs(qs[-1]-qs[0]))),
            'tool_axis_approach_dot':float(Rd[:,2]@np.array([0,0,-1.])),
            'task':task.sanity(),'calibration_hash':b.calibration_hash,'observed_at':snapshot['observed_at'],
            'contact_frame_qualified':False,'collision_qualified':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    r=check(json.loads(a.snapshot.read_text()));r['input_sha256']=hashlib.sha256(a.snapshot.read_bytes()).hexdigest();a.output.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
