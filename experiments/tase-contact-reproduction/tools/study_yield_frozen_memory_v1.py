"""FM-v1: two mechanically matched MSFC identity-metric frozen-observer cells."""
import argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime,timezone
from run_contact_yield import read,write
from study_yield_observer_transfer_v1 import job,sha,ROOT

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 source=read(ROOT/'runs/yield-frozen-transfer-v1/protocol.json');gm=read(ROOT/'runs/yield-gain-memory-v1/protocol.json')['variants']
 params=gm['MSFC-GM-v1-g50-identity_metric'];on=source['parameters']['MSFC'];assert {k:v for k,v in params.items() if k!='minimum_metric_eigenvalue'}=={k:v for k,v in on.items() if k!='minimum_metric_eigenvalue'}
 retained=[r for r in read(ROOT/'runs/yield-frozen-transfer-v1/summary.json')['runs'] if r['method']=='MSFC'];assert len(retained)==2
 for r in retained:assert sha(Path(r['path']))==r['sha256']
 protocol={'version':'FM-v1','dataset_role':'development','observer':source['observer'],'parameters':params,'retained_on':retained,
  'only_parameter_change':'minimum_metric_eigenvalue -> 1.0 (existing identity metric ablation, filters retain their own states)',
  'source_hashes':{p.name:sha(p) for p in (ROOT/'tools').glob('contact_yield_*.py')},'physical_qualification':False};write(a.output/'protocol.json',protocol)
 manifest={'started_at':datetime.now(timezone.utc).isoformat(),'workers':2,'exit_code':None};write(a.output/'start.json',manifest)
 try:
  with ProcessPoolExecutor(max_workers=2) as pool:results=list(pool.map(job,[(a.output,'MSFC',params,s,source['observer']) for s in ('nominal','sustained_release_tangent')]))
  write(a.output/'summary.json',{'runs':results,'dataset_role':'development'});manifest['exit_code']=0
 except BaseException:manifest['exit_code']=1;raise
 finally:manifest['finished_at']=datetime.now(timezone.utc).isoformat();write(a.output/'manifest.json',manifest)
if __name__=='__main__':main()
