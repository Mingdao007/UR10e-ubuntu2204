"""FP-v1: original half-second oblique pulse under frozen observer, no retuning."""
import argparse,hashlib
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime,timezone
from run_contact_yield import read,write
from study_yield_observer_transfer_v1 import job,sha,ROOT
from contact_yield_protocol import parse_scenario

def run(spec):
 out,label,method,params,observer=spec
 row=job((out/label,method,params,'short_pulse_oblique',observer));return {'variant':label,**row}
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 ft=read(ROOT/'runs/yield-frozen-transfer-v1/protocol.json');params=ft['parameters'];variants={m:{'method':m,'parameters':params[m]} for m in ('SFC','DSFC','MSFC')}
 variants['MSFC-identity']={'method':'MSFC','parameters':read(ROOT/'runs/yield-frozen-memory-v1/protocol.json')['parameters']}
 nominal={r['method']:r for r in read(ROOT/'runs/yield-frozen-transfer-v1/summary.json')['runs'] if r['scenario']=='nominal'}
 nominal['MSFC-identity']=next(r for r in read(ROOT/'runs/yield-frozen-memory-v1/summary.json')['runs'] if r['scenario']=='nominal')
 for row in nominal.values():assert sha(Path(row['path']))==row['sha256']
 protocol={'version':'FP-v1','dataset_role':'development','variants':variants,'observer':ft['observer'],'nominal':nominal,'scenario':parse_scenario('short_pulse_oblique',timeline='full_cycle'),
  'event_window_s':[20.,23.],'interpretation':'Existing 3N raised-cosine pulse, 20-20.5s. Frozen observer is a mechanism ablation. Same parameters, no tuning or holdout. Full metrics plus fixed event-window diagnostics.',
  'source_hashes':{p.name:sha(p) for p in (ROOT/'tools').glob('contact_yield_*.py')}};write(a.output/'protocol.json',protocol)
 manifest={'contract_id':'ur10e_concurrency_contract_v1','task':'FP-v1 four frozen-observer pulse cells',
  'dependencies':['FT-v1 matched nominals','FM-v1 identity-metric nominal'],'resource_lane':'CPU throughput',
  'claim_class':'offline development diagnostic','output_paths':[str(a.output.resolve())],
  'started_at':datetime.now(timezone.utc).isoformat(),'workers':2,'exit_code':None,'live_executed':False};write(a.output/'start.json',manifest)
 try:
  with ProcessPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[(a.output,label,x['method'],x['parameters'],ft['observer']) for label,x in variants.items()]))
  write(a.output/'summary.json',{'runs':results,'dataset_role':'development'});manifest['exit_code']=0
 except BaseException:manifest['exit_code']=1;raise
 finally:
  manifest['finished_at']=datetime.now(timezone.utc).isoformat();write(a.output/'manifest.json',manifest)
  write(a.output/'parallel_run_manifest.json',manifest)
if __name__=='__main__':main()
