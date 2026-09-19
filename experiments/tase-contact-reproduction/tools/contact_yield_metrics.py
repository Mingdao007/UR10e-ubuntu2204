"""Evaluator-only load metrics and matched nominal/intervention comparisons."""
import math
import numpy as np
from contact_yield_protocol import parse_scenario

FORCE_LIMIT_N=8. # Diagnostic threshold, not a material damage/safety bound.
RECOVERY_FORCE_BAND_N=.5
RECOVERY_PATH_BAND_M=.002

def summarize_trial(rows,*,failed=False,failure_message=None,scenario='nominal',material='stiff_low_mu',
                    method='SFC',dt_s=.002,kinematics_kind='unknown',campaign_kind='mechanism_seed',timeline='diagnostic'):
    n=len(rows)
    get=lambda k:np.asarray([r[k] for r in rows],dtype=float)
    load=get('true_normal_load_n');err=get('force_error_n');path=get('path_error_m')
    angle=get('orientation_error_rad');v=get('actual_progress_m_s');vr=get('reference_progress_m_s')
    rms=lambda a:float(np.sqrt(np.mean(a*a))) if n else None
    peak=lambda a:float(np.max(a)) if n else None
    requested=float(np.sum(vr)*dt_s);progress=float(np.sum(v)*dt_s)
    return {'method':method,'scenario':scenario,'material':material,'n_samples':n,'failed':bool(failed),
        'failure_message':failure_message,'objective_eligible':bool(n and not failed),
        'force_mae_n':float(np.mean(abs(err))) if n else None,'force_rmse_n':rms(err),
        'force_peak_n':peak(load),'force_error_peak_n':peak(abs(err)),
        'force_overlimit_duration_s':float(np.sum(load>FORCE_LIMIT_N)*dt_s),
        'force_diagnostic_limit_n':FORCE_LIMIT_N,'contact_loss_duration_s':float(np.sum(load<1.)*dt_s),
        'path_rmse_m':rms(path),'path_peak_m':peak(path),'orientation_rmse_rad':rms(angle),
        'orientation_peak_rad':peak(angle),'normal_estimation_rmse_rad':rms(get('normal_estimation_error_rad')),
        'reference_progress_m':requested,'actual_progress_m':progress,
        'progress_ratio':progress/requested if requested>1e-12 else None,
        'saturation_ticks':sum(bool(r['saturated']) for r in rows),
        'qp_intervention_ticks':sum(bool(r['qp_intervention']) for r in rows),
        'residual_path_m':float(path[-1]) if n else None,
        'recovery_s':None,'recovery_definition':'computed only against a matched nominal trial after release',
        'kinematics_kind':kinematics_kind,'campaign_kind':campaign_kind,'claim_scope':'simulation only'}

def compare_pair(nominal,disturbed):
    keys=('method','material','dt_s','duration_s','preparation','timeline','identity')
    if any(nominal[k]!=disturbed[k] for k in keys):raise ValueError('unmatched nominal/disturbed pair')
    if nominal['scenario']!='nominal' or disturbed['scenario']=='nominal':raise ValueError('pair needs nominal and disturbance')
    if nominal['metrics']['failed'] or disturbed['metrics']['failed']:
        return {'eligible':False,'reason':'failed member; retained, no performance ranking'}
    a,b=nominal['rows'],disturbed['rows']
    if len(a)!=len(b) or not a:raise ValueError('pair sample grids differ')
    t=np.asarray([r['time_s'] for r in b]);ta=np.asarray([r['time_s'] for r in a])
    if not np.allclose(t,ta,atol=1e-12,rtol=0):raise ValueError('pair clocks differ')
    spec=parse_scenario(disturbed['scenario'],timeline=disturbed['timeline'])
    release=spec['start_s']+spec['width_s']+spec['hold_s']+spec['release_s']
    delta=np.asarray([r['position_m'] for r in b])-np.asarray([r['position_m'] for r in a])
    distance=np.linalg.norm(delta,axis=1)
    force=np.asarray([r['true_normal_load_n'] for r in b])-np.asarray([r['true_normal_load_n'] for r in a])
    active=(t>=spec['start_s'])&(t<release);post=t>=release
    result={'eligible':bool(np.any(active) and np.any(post)),'release_s':release,
        'yield_peak_m':float(max(distance[active])) if np.any(active) else None,
        'residual_displacement_m':float(distance[-1]),'recovery_s':None,'recoil_m':None,
        'force_peak_n':disturbed['metrics']['force_peak_n'],'matched_nominal_force_peak_n':nominal['metrics']['force_peak_n'],
        'definition':'measured TCP difference versus same-method nominal; recovery stays within 2mm and 0.5N through remaining window',
        'right_censored':True}
    if not np.any(post):return result
    idx=np.where(post)[0];at_release=delta[idx[0]];norm=np.linalg.norm(at_release)
    if norm>1e-12:result['recoil_m']=float(max(0.,-np.min(delta[idx]@(at_release/norm))))
    else:result['recoil_m']=0.
    good=(distance<=RECOVERY_PATH_BAND_M)&(abs(force)<=RECOVERY_FORCE_BAND_N)
    # Require at least 0.1s remaining; never report initial/pre-intervention settling as recovery.
    suffix=np.logical_and.accumulate(good[::-1])[::-1]
    candidates=np.where(post&suffix&(t<=t[-1]-.1))[0]
    if len(candidates):result.update(recovery_s=float(t[candidates[0]]-release),right_censored=False)
    return result
