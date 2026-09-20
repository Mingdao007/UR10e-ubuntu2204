"""FT-v1: four fixed frozen-observer cells; reuse DSFC nominal/tangent pair."""
import argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime,timezone
from run_contact_yield import read,write
from study_yield_observer_transfer_v1 import job,sha,ROOT

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 old=read(ROOT/'runs/yield-observer-transfer-v1/protocol.json');params=old['parameters']
 no=read(ROOT/'runs/yield-normal-v3/protocol.json');observer=next(c['observer'] for c in no['cells'] if c['id']=='frozen-tangent-hold')
 index={r['id']:r for r in read(ROOT/'report/yield-normal-v3/results.json')['rows']}
 retained=[{'method':'DSFC','scenario':s,**{k:index[n][k] for k in ('path','sha256','full_cycle','metrics')}} for n,s in [('retained-approach','nominal'),('frozen-tangent-hold','sustained_release_tangent')]]
 for r in retained:assert sha(Path(r['path']))==r['sha256']
 scenarios=('nominal','sustained_release_tangent');protocol={'version':'FT-v1','observer':observer,'parameters':params,'retained':retained,
  'scenarios':scenarios,'material':'stiff_low_mu','dt_s':.002,'plant_substeps':8,'duration_s':62.83185307179586,
  'dataset_role':'development','interpretation':'Frozen observer is a mechanism ablation, not unknown-surface solution. No tuning or final ranking.',
  'source_hashes':{p.name:sha(p) for p in (ROOT/'tools').glob('contact_yield_*.py')}}
 write(a.output/'protocol.json',protocol);manifest={'started_at':datetime.now(timezone.utc).isoformat(),'workers':2,'exit_code':None};write(a.output/'start.json',manifest)
 try:
  with ProcessPoolExecutor(max_workers=2) as pool:results=list(pool.map(job,[(a.output,m,params[m],s,observer) for m in ('SFC','MSFC') for s in scenarios]))
  write(a.output/'summary.json',{'runs':retained+results,'dataset_role':'development'});manifest['exit_code']=0
 except BaseException:manifest['exit_code']=1;raise
 finally:manifest['finished_at']=datetime.now(timezone.utc).isoformat();write(a.output/'manifest.json',manifest)
if __name__=='__main__':main()
