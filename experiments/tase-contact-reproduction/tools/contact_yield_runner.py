"""Deterministic closed-loop mechanism experiments; never opens a device."""
from __future__ import annotations
from dataclasses import asdict
import hashlib, json, math, time
from pathlib import Path
import numpy as np
from contact_semantics import orientation_axis_angle_error
from contact_yield_controller import YieldController, YieldSettings
from contact_yield_kinematics import load_kinematics
from contact_yield_math import projector_tangent
from contact_yield_metrics import summarize_trial
from contact_yield_protocol import (CLAIM_SCOPE, DEFAULT_DT_S, DIAGNOSTIC_DURATION_S,
    EXPERIMENT_ROOT, PERIOD_S, QP_LIBRARY_PATH, Task, law_seed_parameters, protocol)
from contact_yield_simulator import YieldSimulator, surface_for_contact, substepped_simulator

HOME_PATH=EXPERIMENT_ROOT/'report/contact-six-qp-20260917/preserved-home.json'

def serial(value):
    if isinstance(value,dict): return {k:serial(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [serial(v) for v in value]
    if isinstance(value,np.ndarray): return value.tolist()
    if isinstance(value,np.generic): return value.item()
    return value

def make_system(*,method,material,dt_s,timeline,qp_library=QP_LIBRARY_PATH,
                build_root=None,require_ur10e=True,law_parameters=None,settings=None,plant_substeps=None):
    kinematics=load_kinematics(require_ur10e=require_ur10e)
    home=json.loads(HOME_PATH.read_text())
    q=np.asarray(home['home_q'] if kinematics.kind=='ur10e_calibrated_pinocchio'
                 else [*home['home_pose'][:3],0,0,0],dtype=float)
    pose=kinematics.pose_and_jacobian(q)
    origin=np.asarray(pose['position_m'])
    rotation=np.asarray(pose['rotation']) if kinematics.kind=='ur10e_calibrated_pinocchio' else np.diag([1.,-1.,-1.])
    # Tool +Z is the preserved contact approach, hence the INWARD direction.
    approach=rotation[:,2].copy()
    plant_type=YieldSimulator if plant_substeps is None else substepped_simulator(plant_substeps)
    plant=plant_type(kinematics=kinematics,surface=surface_for_contact(origin,material=material),
        material=material,q=q,timeline=timeline,require_ur10e=require_ur10e,seed_sensor_from_contact=True)
    if kinematics.kind!='ur10e_calibrated_pinocchio':plant.rotation=rotation.copy()
    controller=YieldController(method=method,qp_library=qp_library,approach_inward_base=approach,
        settings=settings or YieldSettings(),law_parameters=law_parameters or law_seed_parameters(method),
        dt_s=dt_s,build_root=build_root)
    return controller,plant,origin

def run_closed_loop(*,method,scenario='nominal',material='stiff_low_mu',duration_s=DIAGNOSTIC_DURATION_S,
        dt_s=DEFAULT_DT_S,timeline='diagnostic',campaign_kind='mechanism_seed',preparation='cold',
        qp_library=QP_LIBRARY_PATH,build_root=None,require_ur10e=True,record_fullstate=True,
        law_parameters=None,settings=None,plant_substeps=None):
    if not math.isfinite(duration_s) or not 0<duration_s<=PERIOD_S:raise ValueError('invalid duration')
    if not math.isfinite(dt_s) or not 0<dt_s<=.004:raise ValueError('invalid dt')
    if preparation not in ('cold','warm'):raise ValueError('unknown preparation')
    if timeline not in ('diagnostic','full_cycle'):raise ValueError('unknown timeline')
    if campaign_kind not in ('mechanism_seed','diagnostic_seed','training','holdout'):raise ValueError('unsupported campaign kind')
    source_paths={p.name:p for p in Path(__file__).parent.glob('contact_yield_*.py')}
    frozen_hashes={name:hashlib.sha256(p.read_bytes()).hexdigest() for name,p in source_paths.items()}
    ctrl,plant,origin=make_system(method=method,material=material,dt_s=dt_s,timeline=timeline,
        qp_library=qp_library,build_root=build_root,require_ur10e=require_ur10e,
        law_parameters=law_parameters,settings=settings,plant_substeps=plant_substeps)
    initial_controller=ctrl.snapshot();initial_plant=plant.snapshot()
    rows=[];records=[];failed=False;failure=None;wall=[];formal_initial=None
    task=Task();entry_ticks=math.ceil(1./dt_s);warm_ticks=math.ceil(2./dt_s) if preparation=='warm' else 0
    path_ticks=math.ceil(duration_s/dt_s)
    phases=[('baseline',i*dt_s) for i in range(warm_ticks)]+[('entry',i*dt_s) for i in range(entry_ticks)]+[('path',i*dt_s) for i in range(path_ticks)]
    try:
        for phase,clock in phases:
            ref=task.reference(clock) if phase=='path' else task.entry_reference(min(clock,1.)) if phase=='entry' else {'position_m':(0,0,0),'velocity_m_s':(0,0,0)}
            reference={'phase':phase,'path_time_s':clock if phase=='path' else None,'force_n':5.,
                'position_m':tuple(origin+np.asarray(ref['position_m'])), 'velocity_m_s':ref['velocity_m_s']}
            active_scenario=scenario if phase=='path' else 'nominal'
            packed=plant.observe(path_time_s=clock if phase=='path' else 0.,scenario=active_scenario,
                reference_velocity_m_s=reference['velocity_m_s'])
            before_c=ctrl.snapshot();before_p=plant.snapshot()
            if phase=='path' and formal_initial is None:formal_initial={'controller':before_c,'plant':before_p}
            start=time.perf_counter()
            try:
                result=ctrl.step(packed['observation'],reference,dt_s)
                after=plant.step(dt_s=dt_s,qdot_cmd=result['qdot_rad_s'],scenario=active_scenario,
                    path_time_s=clock if phase=='path' else 0.,reference_velocity_m_s=reference['velocity_m_s'])
            except Exception:
                ctrl.restore(before_c);plant.restore(before_p);raise
            wall.append(time.perf_counter()-start)
            obs=packed['observation'];truth=packed['evaluator']
            if phase=='path':
                true_in=np.asarray(truth['true_inward_normal_base']); tangent=projector_tangent(true_in)
                velocity=np.asarray(obs['linear_velocity_base_m_s']);rv=np.asarray(reference['velocity_m_s']);speed=np.linalg.norm(tangent@rv)
                direction=tangent@rv/max(speed,1e-15)
                rows.append({'time_s':clock,'dt_s':dt_s,'force_error_n':truth['true_normal_load_n']-5.,
                    'true_normal_load_n':truth['true_normal_load_n'],'estimated_force_error_n':result['force_error_n'],
                    'path_error_m':float(np.linalg.norm(tangent@(np.asarray(obs['position_m'])-np.asarray(reference['position_m'])))),
                    'position_m':obs['position_m'],'rotation':obs['rotation'],
                    'reference_position_m':reference['position_m'],'normal_estimate':result['inward_normal_base'],
                    'true_inward_normal':true_in,'external_force_base_n':truth['external_force_base_n'],
                    'orientation_error_rad':orientation_axis_angle_error(obs['rotation'],true_in),
                    'normal_estimation_error_rad':float(np.arccos(np.clip(true_in@np.asarray(result['inward_normal_base']),-1,1))),
                    'reference_progress_m_s':float(speed),'actual_progress_m_s':float(direction@velocity),
                    'saturated':bool(result['normal_saturated'] or result['tangent_saturation_m_s']>0),
                    'qp_intervention':bool(result['qp_intervention']), 'task_scale':result['task_scale']})
            if record_fullstate:
                records.append({'dt_s':dt_s,'scenario':active_scenario,'plant_path_time_s':clock if phase=='path' else 0.,
                    'observation':obs,'reference':reference,'result':result,'controller_snapshot':ctrl.snapshot(),
                    'simulator_snapshot':plant.snapshot(),'observation_after':after['observation']})
    except Exception as error:
        failed=True;failure=f'{type(error).__name__}: {error}'
    hashes={name:hashlib.sha256(p.read_bytes()).hexdigest() for name,p in source_paths.items()}
    if hashes!=frozen_hashes:failed=True;failure='source changed during experiment; evidence not qualified'
    metrics=summarize_trial(rows,failed=failed,failure_message=failure,scenario=scenario,material=material,
        method=method,dt_s=dt_s,kinematics_kind=plant.kinematics.kind,campaign_kind=campaign_kind,timeline=timeline)
    artifact={'schema':'ur10e.contact-yield-run-v2','method':method,'role':ctrl.role,'scenario':scenario,
        'material':material,'timeline':timeline,'duration_s':duration_s,'dt_s':dt_s,'preparation':preparation,
        'preparation_protocol':{'warm_duration_s':warm_ticks*dt_s,'entry_duration_s':entry_ticks*dt_s,'no_state_reset':True},
        'kinematics_kind':plant.kinematics.kind,'kinematics_claim_scope':plant.kinematics.claim_scope,
        'identity':ctrl.identity,'identity_payload':ctrl.identity_payload,'plant_identity_payload':plant.identity_payload,'protocol_sha256':protocol()['sha256'],
        'source_hashes':frozen_hashes,'metrics':metrics,'rows':rows,'records':records,
        'initial_controller_snapshot':initial_controller,'initial_simulator_snapshot':initial_plant,
        'formal_initial_snapshot':formal_initial,'final_controller_snapshot':ctrl.snapshot(),'final_simulator_snapshot':plant.snapshot(),
        'full_cycle':not failed and timeline=='full_cycle' and math.isclose(duration_s,PERIOD_S,abs_tol=1e-9),
        'timing_diagnostic':{'tick_p99_s':float(np.quantile(wall,.99)) if wall else None,'tick_max_s':max(wall) if wall else None,
                             'includes_plant':True,'physical_timing_qualification':False},
        'campaign_kind':campaign_kind,'formal_campaign_complete':False,'claim_scope':CLAIM_SCOPE}
    ctrl.close()
    return serial(artifact)
