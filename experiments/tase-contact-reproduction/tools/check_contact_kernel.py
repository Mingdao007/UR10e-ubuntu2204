#!/usr/bin/env python3
"""Feature-bearing offline law/task/QP smoke; prescribed inputs are not a plant."""
import argparse,json,math,hashlib,datetime
from pathlib import Path
import numpy as np
from contact_laws import ContactLaw,PUBLIC_LAWS
from contact_benchmark_kernel import ContactKernel
from contact_benchmark_protocol import Task,disturbance
from step5c_calibrated_kinematics_audit import build_calibrated_model,rotvec_to_matrix
from step5d_autotune_v4_r004.calibrated_runtime import tcp_jacobian_base


def run(snapshot_path,library,output):
    source_paths=[Path(__file__),*[Path(__file__).parent/name for name in ('contact_benchmark_kernel.py','contact_benchmark_outer.py','contact_benchmark_protocol.py','contact_laws.py','contact_qp.py','build_contact_laws.py')],library,snapshot_path]
    sources={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    started=datetime.datetime.now(datetime.timezone.utc).isoformat()
    snapshot=json.loads(snapshot_path.read_text());obs=snapshot['rtde'];model=build_calibrated_model()
    J=tcp_jacobian_base(model,obs['actual_q']);anchor=np.array(obs['actual_TCP_pose'][:3]);R=rotvec_to_matrix(np.asarray(obs['actual_TCP_pose'][3:]));task=Task();results=[]
    # A complete task clock is replayed with prescribed measured positions and
    # forces. This validates composition/finite outputs, not closed-loop benefit.
    for name in PUBLIC_LAWS:
        with ContactLaw.from_config(name) as law:
            k=ContactKernel(law=law,qp_library=library,anchor_m=anchor,task_basis=np.eye(3),target_rotation=R,
                            raw_force_limit_n=20.,raw_torque_limit_nm=2.,qp_deadline_s=None,kernel_deadline_s=None)
            times=[];qmax=0.;capcount=0;resmax=0.;failure=None;count=0
            for i in range(math.floor(task.duration_s/.002)+1):
                t=i*.002;ref=task.reference(t)
                try:
                    r=k.step(jacobian=J,joint_velocity_lower=np.full(6,-.05),joint_velocity_upper=np.full(6,.05),
                        time_s=t,dt_s=.002,position_m=anchor+ref['position_m'],rotation=R,
                        raw_force_base_n=(.2*math.sin(.5*t),0.,5.+.1*math.sin(t)),raw_torque_base_nm=(0,0,0),
                        injection_task_n=disturbance('oblique_double',t,amplitude_n=2.))
                except Exception as exc:
                    failure={'tick':i,'time_s':t,'error':str(exc)};break
                count+=1;times.append(r['kernel_wall_s']);qmax=max(qmax,max(abs(x) for x in r['qdot_rad_s']));capcount+=r['velocity_cap_intervention_m_s']>1e-10;resmax=max(resmax,r['qp_equality_residual'])
            results.append({'controller':name,'samples':count,'failure':failure,'maximum_qdot_rad_s':qmax,
                    'capped_ticks':capcount,'maximum_qp_equality_residual':resmax,
                    'kernel_seconds':{key:float(np.percentile(times,p)) for key,p in [('p50',50),('p99',99),('max',100)]} if times else {},
                    'over_2ms_ticks':sum(t>.002 for t in times)})
            print(json.dumps(results[-1]),flush=True)
    payload={'schema':'contact-kernel-prescribed-input-smoke-v1','claim_scope':'full task clock on prescribed observations at one fixed Jacobian; NOT plant simulation, live timing qualification or controller comparison',
             'raw_guard_limits_are_fixture_values':True,'live_authority':False,'results':results,
             'started_at':started,'ended_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'sources':sources,
             'sources_unchanged':sources=={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}}
    output.write_text(json.dumps(payload,indent=2)+'\n')
    return payload['sources_unchanged'] and all(r['failure'] is None for r in results)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--qp-library',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    raise SystemExit(0 if run(a.snapshot,a.qp_library,a.output) else 1)
