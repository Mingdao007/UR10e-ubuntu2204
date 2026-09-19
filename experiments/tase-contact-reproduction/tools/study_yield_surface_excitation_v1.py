"""SE-v1: four preregistered development cells increase only evaluator curvature."""
import argparse,hashlib,json
from pathlib import Path
from datetime import datetime,timezone
from concurrent.futures import ProcessPoolExecutor
import numpy as np
from contact_yield_runner import run_closed_loop,make_system
from contact_yield_protocol import PERIOD_S,Task
from run_contact_yield import read,write
ROOT=Path(__file__).resolve().parents[1]
SURFACE={'kappa_xx':6.,'kappa_yy':8.}
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()
def job(spec):
    output,material,variant,params,observer=spec
    r=run_closed_loop(method='DSFC',material=material,scenario='nominal',duration_s=PERIOD_S,
        timeline='full_cycle',plant_substeps=8,law_parameters=params,estimator_parameters=observer,
        surface_parameters=SURFACE)
    path=output/f'DSFC-{material}-{variant}.json.gz';write(path,r)
    def rms(field):return float(np.rad2deg(np.sqrt(np.mean([v[field]**2 for v in r['rows']])))) if r['rows'] else None
    approach=np.array(r['initial_controller_snapshot']['normal_estimate']['approach_inward_base'])
    angles=np.array([np.arccos(np.clip(np.dot(v['true_inward_normal'],approach),-1,1)) for v in r['rows']])
    return {'material':material,'variant':variant,'path':str(path.resolve()),'sha256':sha(path),
        'full_cycle':r['full_cycle'],'metrics':r['metrics'],'normal_rms_deg':rms('normal_estimation_error_rad'),
        'orientation_rms_deg':rms('orientation_error_rad'),
        'true_normal_to_approach_rms_deg':float(np.rad2deg(np.sqrt(np.mean(angles**2)))) if len(angles) else None,
        'true_normal_to_approach_max_deg':float(np.rad2deg(max(angles))) if len(angles) else None}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    output=a.output.resolve();output.mkdir(parents=True,exist_ok=False)
    params=read(ROOT/'config/contact_yield_candidates/dsfc.json');observers={'legacy':{},'frozen_prior':read(ROOT/'config/yield_normal_frozen_prior_v1.json')}
    # Planned normal change is evaluated offline along the fixed reference, not supplied to controller.
    c,plant,origin=make_system(method='DSFC',material='stiff_low_mu',dt_s=.002,timeline='full_cycle',surface_parameters=SURFACE)
    try:
        approach=c.approach_inward_base;angles=[]
        for t in np.linspace(0,PERIOD_S,1001):
            pos=origin+np.asarray(Task().reference(float(t))['position_m'])
            normal=-plant.surface.true_outward_normal(pos)
            angles.append(float(np.rad2deg(np.arccos(np.clip(normal@approach,-1,1)))))
    finally:c.close()
    protocol={'version':'SE-v1','dataset_role':'development','method':'DSFC','parameters':params,'surface_parameters':SURFACE,
        'observers':observers,'materials':['stiff_low_mu','compliant_high_mu'],'scenario':'nominal',
        'controller_dt_s':.002,'plant_substeps':8,'duration_s':PERIOD_S,'physical_reference_changed':False,
        'planned_normal_to_approach_rms_deg':float(np.sqrt(np.mean(np.array(angles)**2))),
        'planned_normal_to_approach_max_deg':max(angles),
        'hypothesis':'Increasing surface-normal variation exposes the limitation of a frozen favorable prior, while legacy force bias may remain confounded by friction.',
        'interpretation':'Report all force/path/attitude/progress/failure costs without tuning. Does not establish intervention robustness or a proposal winner.',
        'source_hashes':{p.name:sha(p) for p in (ROOT/'tools').glob('contact_yield_*.py')}}
    write(output/'protocol.json',protocol)
    manifest={'contract_id':'ur10e_concurrency_contract_v1','task':'SE-v1 surface excitation','resource_lane':'CPU throughput',
        'workers':4,'dependencies':['five surface/replay focused tests'],'claim_class':'simulator_only_development',
        'started_at':utc(),'exit_code':None,'output_paths':[str(output)]};write(output/'parallel_run_start.json',manifest)
    try:
        specs=[(output,m,v,params,o) for m in protocol['materials'] for v,o in observers.items()]
        with ProcessPoolExecutor(max_workers=4) as pool:
            rows=[]
            for row in pool.map(job,specs):rows.append(row);print(row['material'],row['variant'],row['normal_rms_deg'],row['metrics']['failed'],flush=True)
        write(output/'summary.json',{'runs':rows,'dataset_role':'development','live_executed':False});manifest['exit_code']=0
    except BaseException:manifest['exit_code']=1;raise
    finally:manifest['finished_at']=utc();write(output/'parallel_run_manifest.json',manifest)
if __name__=='__main__':main()
