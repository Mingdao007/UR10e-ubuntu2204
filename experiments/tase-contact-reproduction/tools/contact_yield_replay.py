"""Fresh-instance forward replay and common-time step refinement."""
import math
import numpy as np
from contact_yield_runner import make_system
from contact_yield_controller import YieldSettings

def differences(actual,expected,path='',atol=1e-9):
    if isinstance(expected,dict):
        if not isinstance(actual,dict) or set(actual)!=set(expected):return [path+' keys']
        return [p for key in expected for p in differences(actual[key],expected[key],path+'/'+key,atol)]
    if isinstance(expected,(list,tuple)):
        if not isinstance(actual,(list,tuple)) or len(actual)!=len(expected):return [path+' shape']
        return [p for i,(a,b) in enumerate(zip(actual,expected)) for p in differences(a,b,path+'/'+str(i),atol)]
    if isinstance(expected,(float,int)) and not isinstance(expected,bool):
        return [] if math.isclose(float(actual),float(expected),abs_tol=atol,rel_tol=0) else [path]
    return [] if actual==expected else [path]

def replay_artifact(artifact):
    if not artifact['records']:raise ValueError('full state records required')
    ident=artifact['identity_payload']
    controller,plant,_=make_system(method=artifact['method'],material=artifact['material'],dt_s=artifact['dt_s'],
        timeline=artifact['timeline'],qp_library=ident['qp_library'],law_parameters=ident['parameters'],
        settings=YieldSettings(**ident['settings']),require_ur10e=artifact['kinematics_kind']=='ur10e_calibrated_pinocchio',
        plant_substeps=artifact['plant_identity_payload'].get('integration_substeps'),
        estimator_parameters=ident['estimator_parameters'],
        surface_parameters=artifact.get('surface_parameters'))
    controller.restore(artifact['initial_controller_snapshot']);plant.restore(artifact['initial_simulator_snapshot'])
    mismatch=[]
    try:
        for i,record in enumerate(artifact['records']):
            result=controller.step(record['observation'],record['reference'],record['dt_s'])
            packed=plant.step(dt_s=record['dt_s'],qdot_cmd=result['qdot_rad_s'],scenario=record['scenario'],
                path_time_s=record['plant_path_time_s'],reference_velocity_m_s=record['reference']['velocity_m_s'],
                software_injection_base_n=record['observation']['software_injection_base_n'],state_age_s=record['observation']['state_age_s'])
            diff=differences(controller.snapshot(),record['controller_snapshot'],'controller')
            diff+=differences(plant.snapshot(),record['simulator_snapshot'],'plant')
            # Compare all persistent state, plus current command. Wall timings are not state.
            diff+=differences(list(result['qdot_rad_s']),record['result']['qdot_rad_s'],'qdot')
            if diff:mismatch.append({'sample':i,'fields':diff})
    finally:controller.close()
    return {'passed':not mismatch,'samples':len(artifact['records']),'mismatches':mismatch,
            'claim_scope':'fresh-instance full-state forward replay; same implementation, not independent physics proof'}

def refinement_error(coarse,fine,*,coarse_dt_s,fine_dt_s):
    ratio=coarse_dt_s/fine_dt_s
    if fine_dt_s<=0 or not math.isclose(ratio,round(ratio),abs_tol=1e-12):raise ValueError('incompatible refinement steps')
    stride=int(round(ratio))
    if not coarse or len(fine)<(len(coarse)-1)*stride+1:raise ValueError('incomplete refinement traces')
    pairs=list(zip(coarse,fine[::stride]))
    if any(not math.isclose(a['time_s'],b['time_s'],abs_tol=1e-10) for a,b in pairs):raise ValueError('refinement clocks differ')
    return {'n_compared':len(pairs),
        'force_error_max_n':max(abs(a['force_error_n']-b['force_error_n']) for a,b in pairs),
        'position_max_m':max(float(np.linalg.norm(np.asarray(a['position_m'])-b['position_m'])) for a,b in pairs),
        'orientation_error_max_rad':max(abs(a['orientation_error_rad']-b['orientation_error_rad']) for a,b in pairs),
        'claim_scope':'step refinement diagnostic; errors reported without automatic acceptance'}
