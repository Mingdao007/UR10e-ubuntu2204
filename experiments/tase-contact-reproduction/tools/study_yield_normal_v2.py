"""NO-v2: four frozen DSFC cells isolate a common observer change."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import numpy as np
from contact_yield_runner import run_closed_loop
from contact_yield_protocol import PERIOD_S
from run_contact_yield import read, write

ROOT=Path(__file__).resolve().parents[1]

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()

def job(spec):
    out,material,scenario,params,observer=spec
    r=run_closed_loop(method='DSFC',material=material,scenario=scenario,duration_s=PERIOD_S,
        timeline='full_cycle',law_parameters=params,estimator_parameters=observer,plant_substeps=8)
    p=out/f'DSFC-{material}-{scenario}.json.gz';write(p,r)
    return {'material':material,'scenario':scenario,'path':str(p.resolve()),'sha256':sha(p),
        'metrics':r['metrics'],'full_cycle':r['full_cycle'],
        'normal_rms_deg':float(np.rad2deg(np.sqrt(np.mean([x['normal_estimation_error_rad']**2 for x in r['rows']])))),
        'orientation_rms_deg':float(np.rad2deg(np.sqrt(np.mean([x['orientation_error_rad']**2 for x in r['rows']]))))}

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--observer-config',type=Path,default=ROOT/'config/yield_normal_observer_v2.json');p.add_argument('--version',default='NO-v2');a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    observer=read(a.observer_config);params=read(ROOT/'config/contact_yield_candidates/dsfc.json')
    prior=read(ROOT/'runs/yield-transfer-v1/summary.json')['runs']
    retained=[r for r in prior if r['variant']=='DSFC' and r['scenario'] in ('nominal','sustained_release_normal')]
    assert len(retained)==4
    for r in retained:assert sha(Path(r['path']))==r['sha256']
    protocol={'version':a.version,'dataset_role':'development','method':'DSFC','parameters':params,
        'observer':observer,'retained_baseline':retained,'controller_dt_s':.002,'plant_substeps':8,
        'observer_config':str(a.observer_config.resolve()),'observer_config_sha256':sha(a.observer_config),
        'caution':'joint observer change; separate term ablation still required; no independent holdout',
        'source_hashes':{f.name:sha(f) for f in (ROOT/'tools').glob('contact_yield_*.py')}}
    write(a.output/'protocol.json',protocol)
    manifest={'contract_id':'ur10e_concurrency_contract_v1','task':'NO-v2 observer development',
        'dependencies':['TR-v1 DSFC baseline','NO-v2 focused tests'],'resource_lane':'CPU throughput',
        'workers':4,'claim_class':'simulator_only_development','started_at':utc(),'exit_code':None,
        'output_paths':[str(a.output.resolve())]}
    write(a.output/'parallel_run_start.json',manifest)
    specs=[(a.output,r['material'],r['scenario'],params,observer) for r in retained]
    try:
        with ProcessPoolExecutor(max_workers=4) as pool:
            results=[]
            for row in pool.map(job,specs):
                results.append(row);print(row['material'],row['scenario'],row['normal_rms_deg'],row['metrics']['failed'],flush=True)
        write(a.output/'summary.json',{'runs':results,'dataset_role':'development','live_executed':False})
        manifest['exit_code']=0
    except BaseException:
        manifest['exit_code']=1;raise
    finally:
        manifest['finished_at']=utc();write(a.output/'parallel_run_manifest.json',manifest)

if __name__=='__main__':main()
