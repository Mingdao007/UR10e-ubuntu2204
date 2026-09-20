"""OT-v1: six fixed SFC/MSFC cells, reuse three DSFC NO-v3 cells."""
import argparse,hashlib
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime,timezone
from run_contact_yield import read,write
from contact_yield_runner import run_closed_loop
from contact_yield_protocol import PERIOD_S
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def job(spec):
 out,method,params,scenario,observer=spec
 r=run_closed_loop(method=method,law_parameters=params,material='stiff_low_mu',scenario=scenario,
    duration_s=PERIOD_S,timeline='full_cycle',plant_substeps=8,estimator_parameters=observer)
 path=out/f'{method}-{scenario}.json.gz';write(path,r)
 return {'method':method,'scenario':scenario,'path':str(path.resolve()),'sha256':sha(path),'full_cycle':r['full_cycle'],'metrics':r['metrics']}
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 prior=read(ROOT/'runs/yield-normal-v3/protocol.json');observer=prior['cells'][0]['observer']
 params={'SFC':read(ROOT/'config/contact_yield_candidates/sfc.json'),'DSFC':read(ROOT/'config/contact_yield_candidates/dsfc.json'),
  'MSFC':read(ROOT/'runs/yield-gain-memory-v1/protocol.json')['variants']['MSFC-GM-v1-g50-on']}
 mapping={'combined-mild-approach':'nominal','combined-normal-hold':'sustained_release_normal','combined-tangent-hold':'sustained_release_tangent'}
 retained=[{'method':'DSFC','scenario':mapping[r['id']],**{k:r[k] for k in ('path','sha256','full_cycle','metrics')}} for r in read(ROOT/'runs/yield-normal-v3/summary.json')['runs'] if r['id'] in mapping]
 for row in retained:assert sha(Path(row['path']))==row['sha256']
 assert len(retained)==3
 scenarios=list(mapping.values());protocol={'version':'OT-v1','dataset_role':'development','observer':observer,'parameters':params,
  'scenarios':scenarios,'material':'stiff_low_mu','dt_s':.002,'plant_substeps':8,'duration_s':PERIOD_S,'retained':retained,
  'source_hashes':{p.name:sha(p) for p in (ROOT/'tools').glob('contact_yield_*.py')},
  'interpretation':'No tuning or observer promotion. Existing seed/candidate parameters, not equal-budget optimization or final ranking. All failures and recovery costs retained. No physical qualification.'}
 write(a.output/'protocol.json',protocol)
 manifest={'started_at':datetime.now(timezone.utc).isoformat(),'workers':3,'new_cells':6,'retained_cells':3,'exit_code':None,'live_executed':False}
 write(a.output/'start.json',manifest)
 try:
  results=[]
  with ProcessPoolExecutor(max_workers=3) as pool:
   for row in pool.map(job,[(a.output,m,params[m],s,observer) for m in ('SFC','MSFC') for s in scenarios]):
    results.append(row);print(row['method'],row['scenario'],row['metrics']['failed'],flush=True)
  write(a.output/'summary.json',{'runs':retained+results,'dataset_role':'development'});manifest['exit_code']=0
 except BaseException:manifest['exit_code']=1;raise
 finally:manifest['finished_at']=datetime.now(timezone.utc).isoformat();write(a.output/'manifest.json',manifest)
if __name__=='__main__':main()
