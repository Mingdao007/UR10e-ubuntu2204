"""DC-v1: separate controller dt from plant step; clarify low-load vs separation."""
import argparse,hashlib,gc
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from datetime import datetime,timezone
import numpy as np
from run_contact_yield import read,write
from contact_yield_runner import run_closed_loop
from contact_yield_controller import YieldSettings
from contact_yield_simulator import SurfaceField
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def describe(r,path):
 surface=SurfaceField(**r['plant_identity_payload']['surface']);loads=np.asarray([x['true_normal_load_n'] for x in r['rows']]);gaps=np.asarray([surface.gap_m(x['position_m']) for x in r['rows']])
 return {'path':str(path.resolve()),'sha256':sha(path),'dt_s':r['dt_s'],'substeps':r['plant_identity_payload']['integration_substeps'],
  'plant_dt_s':r['dt_s']/r['plant_identity_payload']['integration_substeps'],'metrics':r['metrics'],'full_cycle':r['full_cycle'],
  'min_contact_load_n':float(min(loads)) if len(loads) else None,'zero_load_duration_s':float(sum(loads<=0)*r['dt_s']),
  'geometric_separation_duration_s':float(sum(gaps>=0)*r['dt_s']),'max_gap_m':float(max(gaps)) if len(gaps) else None,
  'definition':'Existing contact_loss_duration_s counts load <1N; zero load and nonnegative geometric gap are separate evaluator-only diagnostics.'}
def job(spec):
 output,dt,steps,source=spec
 ident=source['identity_payload']
 r=run_closed_loop(method=source['method'],scenario=source['scenario'],material=source['material'],duration_s=source['duration_s'],
  dt_s=dt,timeline=source['timeline'],preparation=source['preparation'],law_parameters=ident['parameters'],settings=YieldSettings(**ident['settings']),
  qp_library=ident['qp_library'],estimator_parameters=ident['estimator_parameters'],surface_parameters=source.get('surface_parameters'),plant_substeps=steps)
 p=output/f'dt-{dt}-substeps-{steps}.json.gz';write(p,r);return describe(r,p)
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 coarse=ROOT/'runs/yield-observer-transfer-v1/SFC-sustained_release_normal.json.gz';fine=ROOT/'runs/yield-observer-transfer-v1-check/fine.json.gz'
 check=read(ROOT/'runs/yield-observer-transfer-v1-check/protocol.json');fr=read(ROOT/'runs/yield-observer-transfer-v1-check/results.json')
 assert sha(coarse)==check['sha256'] and sha(fine)==fr['fine_sha256']
 source=read(coarse);retained=[describe(source,coarse)];source={k:source[k] for k in ('method','scenario','material','duration_s','timeline','preparation','identity_payload')};gc.collect()
 r=read(fine);retained.append(describe(r,fine));del r;gc.collect()
 protocol={'version':'DC-v1','dataset_role':'development','method':'SFC','new_cells':[{'dt_s':.002,'substeps':16},{'dt_s':.001,'substeps':4}],
  'retained':retained,'source':source,'interpretation':'2x2 controller dt {1,2}ms and plant step {0.125,0.25}ms. No retuning, convergence-order claim or physical acceptance.',
  'source_hashes':{p.name:sha(p) for p in (ROOT/'tools').glob('contact_yield_*.py')}}
 write(a.output/'protocol.json',protocol);manifest={'started_at':datetime.now(timezone.utc).isoformat(),'workers':2,'exit_code':None};write(a.output/'start.json',manifest)
 try:
  with ProcessPoolExecutor(max_workers=2) as pool:results=list(pool.map(job,[(a.output,.002,16,source),(a.output,.001,4,source)]))
  write(a.output/'summary.json',{'runs':retained+results,'dataset_role':'development'});manifest['exit_code']=0
 except BaseException:manifest['exit_code']=1;raise
 finally:manifest['finished_at']=datetime.now(timezone.utc).isoformat();write(a.output/'manifest.json',manifest)
if __name__=='__main__':main()
