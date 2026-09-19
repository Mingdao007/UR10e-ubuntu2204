"""P0-v1: fixed physical task, explicit frozen prior uncertainty; six development cells."""
import argparse,json,hashlib
from pathlib import Path
from datetime import datetime,timezone
from concurrent.futures import ProcessPoolExecutor
import numpy as np
from contact_yield_runner import make_system,run_closed_loop
from contact_yield_protocol import PERIOD_S,Task
from run_contact_yield import read,write
ROOT=Path(__file__).resolve().parents[1]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def job(spec):
    output,material,label,parameters,observer=spec
    result=run_closed_loop(method='DSFC',scenario='nominal',material=material,duration_s=PERIOD_S,
        timeline='full_cycle',law_parameters=parameters,estimator_parameters=observer,plant_substeps=8)
    path=output/f'DSFC-{material}-{label}.json.gz';write(path,result)
    rows=result['rows'];initial=result['initial_controller_snapshot']['normal_estimate']
    drift=[np.arccos(np.clip(np.dot(row['normal_estimate'],initial['inward_normal_base']),-1,1)) for row in rows]
    return {'material':material,'prior':label,'path':str(path.resolve()),'sha256':sha(path),
        'full_cycle':result['full_cycle'],'metrics':result['metrics'],
        'normal_rms_deg':float(np.rad2deg(np.sqrt(np.mean([r['normal_estimation_error_rad']**2 for r in rows])))),
        'orientation_rms_deg':float(np.rad2deg(np.sqrt(np.mean([r['orientation_error_rad']**2 for r in rows])))),
        'normal_drift_max_deg':float(np.rad2deg(max(drift))) if drift else None,
        'controller_identity':result['identity'],'plant_identity':result['plant_identity_payload']}
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    params=read(ROOT/'config/contact_yield_candidates/dsfc.json')
    observer=read(ROOT/'config/yield_normal_frozen_prior_v1.json')
    controller,plant,_=make_system(method='DSFC',material='stiff_low_mu',dt_s=.002,
        timeline='full_cycle',law_parameters=params,plant_substeps=8)
    try:
        approach=controller.approach_inward_base.copy()
        velocity=np.asarray(Task().reference(0.)['velocity_m_s'])
        along=velocity-approach*np.dot(velocity,approach);along/=np.linalg.norm(along)
        across=np.cross(approach,along);across/=np.linalg.norm(across)
    finally:controller.close()
    angle=np.deg2rad(10.)
    priors={'approach':approach,'along_10deg':np.cos(angle)*approach+np.sin(angle)*along,
            'across_10deg':np.cos(angle)*approach+np.sin(angle)*across}
    configurations={label:{**observer,'initial_inward_normal_base':prior.tolist()} for label,prior in priors.items()}
    protocol={'version':'P0-v1','dataset_role':'development','method':'DSFC','law_parameters':params,
        'observer':'frozen explicit prior; not an unknown-surface solution','observer_configurations':configurations,
        'physical_approach_inward_base':approach.tolist(),'physical_task_changed':False,
        'controller_dt_s':.002,'plant_substeps':8,'duration_s':PERIOD_S,'scenario':'nominal',
        'materials':['stiff_low_mu','compliant_high_mu'],
        'hypothesis':'Accurate frozen prior can conceal lack of surface identification; independent prior error changes force/path/attitude performance without changing the physical task.',
        'interpretation':'No winner threshold or retuning. Quantify all task costs, normal drift and failures. This tests prior sensitivity, not varying-normal tracking or intervention robustness.',
        'source_hashes':{p.name:sha(p) for p in (ROOT/'tools').glob('contact_yield_*.py')}}
    write(output/'protocol.json',protocol)
    manifest={'contract_id':'ur10e_concurrency_contract_v1','task':'P0-v1 observer-prior sensitivity',
        'dependencies':['prior-frozen focused tests'],'resource_lane':'CPU throughput','workers':3,
        'claim_class':'simulator_only_development','started_at':utc(),'exit_code':None,
        'output_paths':[str(output)]};write(output/'parallel_run_start.json',manifest)
    try:
        specs=[(output,material,label,params,config) for material in protocol['materials'] for label,config in configurations.items()]
        with ProcessPoolExecutor(max_workers=3) as pool:
            results=[]
            for row in pool.map(job,specs):
                results.append(row);print(row['material'],row['prior'],row['normal_rms_deg'],row['metrics']['failed'],flush=True)
        write(output/'summary.json',{'runs':results,'dataset_role':'development','live_executed':False})
        manifest['exit_code']=0
    except BaseException:
        manifest['exit_code']=1;raise
    finally:
        manifest['finished_at']=utc();write(output/'parallel_run_manifest.json',manifest)
if __name__=='__main__':main()
