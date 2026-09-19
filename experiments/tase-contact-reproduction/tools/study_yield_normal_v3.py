"""NO-v3: nine frozen development cells, one material, no per-cell retuning."""
import argparse,hashlib,json
from pathlib import Path
from datetime import datetime,timezone
from concurrent.futures import ProcessPoolExecutor
import numpy as np
from contact_yield_runner import run_closed_loop
from contact_yield_protocol import PERIOD_S
from contact_yield_simulator import SurfaceField
from run_contact_yield import read,write
ROOT=Path(__file__).resolve().parents[1]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def normal_error(n,truth):return float(np.rad2deg(np.arccos(np.clip(np.dot(n,truth),-1,1))))
def job(spec):
    output,cell,params=spec
    r=run_closed_loop(method='DSFC',material='stiff_low_mu',scenario=cell['scenario'],duration_s=PERIOD_S,
        timeline='full_cycle',plant_substeps=8,law_parameters=params,estimator_parameters=cell['observer'],
        surface_parameters=cell['surface'])
    path=output/f"{cell['id']}.json.gz";write(path,r)
    rows=r['rows'];records=r['records'];surface=SurfaceField(**r['plant_identity_payload']['surface'])
    initial_error=None;path_start_error=None
    if records:
        initial_error=normal_error(r['initial_controller_snapshot']['normal_estimate']['inward_normal_base'],
            -surface.true_outward_normal(records[0]['observation']['position_m']))
        formal=next((x for x in records if x['reference']['phase']=='path'),None)
        if formal is not None:
            path_start_error=normal_error(r['formal_initial_snapshot']['controller']['normal_estimate']['inward_normal_base'],
                -surface.true_outward_normal(formal['observation']['position_m']))
    tail_errors=[x['normal_estimation_error_rad']**2 for x in rows if x['time_s']>=PERIOD_S-10]
    approach=np.asarray(r['initial_controller_snapshot']['normal_estimate']['approach_inward_base'])
    visited_angles=[np.arccos(np.clip(np.dot(x['true_inward_normal'],approach),-1,1)) for x in rows]
    windows={}
    for name,start,end in [('entry',-1.,0.),('path',0.,PERIOD_S),('intervention',20.,35.5),('tail',PERIOD_S-10.,PERIOD_S)]:
        subset=[x for x in records if (x['reference']['phase']=='entry' if name=='entry' else
            x['reference']['phase']=='path' and start<=x['reference']['path_time_s']<end)]
        windows[name]={'samples':len(subset),'gate_open_fraction':float(np.mean([bool(x['result']['estimator']['contact_gate'] and x['result']['estimator']['excitation_gate']) for x in subset])) if subset else None}
        residuals=[x['result']['estimator'].get('coplanarity_residual') for x in subset]
        residuals=[v for v in residuals if v is not None]
        windows[name]['coplanarity_residual_rms']=float(np.sqrt(np.mean(np.square(residuals)))) if residuals else None
        windows[name]['coplanarity_applied_fraction']=float(np.mean([x['result']['estimator'].get('coplanarity_update_applied',False) for x in subset])) if subset else None
    return {'id':cell['id'],'cell':cell,'path':str(path.resolve()),'sha256':sha(path),'full_cycle':r['full_cycle'],
        'metrics':r['metrics'],'initial_normal_error_deg':initial_error,'path_start_normal_error_deg':path_start_error,
        'tail_normal_rms_deg':float(np.rad2deg(np.sqrt(np.mean(tail_errors)))) if tail_errors else None,
        'normal_rms_deg':float(np.rad2deg(np.sqrt(np.mean([x['normal_estimation_error_rad']**2 for x in rows])))) if rows else None,
        'true_normal_to_approach_rms_deg':float(np.rad2deg(np.sqrt(np.mean(np.square(visited_angles))))) if visited_angles else None,
        'true_normal_to_approach_max_deg':float(np.rad2deg(max(visited_angles))) if visited_angles else None,
        'gate_windows':windows}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    output=a.output.resolve();output.mkdir(parents=True,exist_ok=False)
    candidate=read(ROOT/'config/yield_normal_observer_v3.json');frozen=read(ROOT/'config/yield_normal_frozen_prior_v1.json')
    motion_only={**candidate,'coplanarity_gain_s_inv':0.}
    priors=read(ROOT/'runs/yield-observer-prior-v1/protocol.json')['observer_configurations']
    params=read(ROOT/'config/contact_yield_candidates/dsfc.json');cells=[]
    def cell(name,observer,prior='approach',scenario='nominal',surface=None):
        cells.append({'id':name,'observer':{**observer,'initial_inward_normal_base':priors[prior]['initial_inward_normal_base']},
            'prior':prior,'scenario':scenario,'surface':surface})
    cell('combined-mild-approach',candidate)
    cell('combined-mild-along',candidate,'along_10deg')
    cell('combined-mild-across',candidate,'across_10deg')
    cell('combined-strong-approach',candidate,surface={'kappa_xx':6.,'kappa_yy':8.})
    cell('combined-normal-hold',candidate,scenario='sustained_release_normal')
    cell('combined-tangent-hold',candidate,scenario='sustained_release_tangent')
    cell('motion-only-mild-across',motion_only,'across_10deg')
    cell('frozen-normal-hold',frozen,scenario='sustained_release_normal')
    cell('frozen-tangent-hold',frozen,scenario='sustained_release_tangent')
    retained=[x for x in read(ROOT/'runs/yield-observer-prior-v1/summary.json')['runs'] if x['material']=='stiff_low_mu']
    retained += [x for x in read(ROOT/'runs/yield-surface-excitation-v1/summary.json')['runs'] if x['material']=='stiff_low_mu' and x['variant']=='frozen_prior']
    assert len(cells)==9 and len(retained)==4
    for row in retained:assert sha(Path(row['path']))==row['sha256']
    protocol={'version':'NO-v3','dataset_role':'development','method':'DSFC','material':'stiff_low_mu','law_parameters':params,
        'controller_dt_s':.002,'plant_substeps':8,'duration_s':PERIOD_S,'cells':cells,'retained':retained,
        'hypotheses':['combined residuals can correct independent initial prior errors','a changing surface distinguishes following from frozen-prior agreement',
            'normal and tangent interventions can contaminate different residuals','motion-only across-prior ablation tests whether CP contributes beyond closed-loop excitation'],
        'interpretation':'Report every task metric and failure. No single-metric winner, no per-cell retuning, no holdout, no physical authorization. No actual cross-slide intervention or compliant-material transfer in this first bounded matrix.',
        'source_hashes':{p.name:sha(p) for p in (ROOT/'tools').glob('contact_yield_*.py')},'observer_config_sha256':sha(ROOT/'config/yield_normal_observer_v3.json')}
    write(output/'protocol.json',protocol)
    manifest={'contract_id':'ur10e_concurrency_contract_v1','task':'NO-v3 combined observer','resource_lane':'CPU throughput','workers':3,
        'dependencies':['integrated NO-v3 focused tests'],'claim_class':'simulator_only_development','started_at':utc(),'exit_code':None,'output_paths':[str(output)]};write(output/'parallel_run_start.json',manifest)
    try:
        with ProcessPoolExecutor(max_workers=3) as pool:
            results=[]
            for row in pool.map(job,[(output,x,params) for x in cells]):results.append(row);print(row['id'],row['normal_rms_deg'],row['metrics']['failed'],flush=True)
        write(output/'summary.json',{'runs':results,'dataset_role':'development','live_executed':False});manifest['exit_code']=0
    except BaseException:manifest['exit_code']=1;raise
    finally:manifest['finished_at']=utc();write(output/'parallel_run_manifest.json',manifest)
if __name__=='__main__':main()
