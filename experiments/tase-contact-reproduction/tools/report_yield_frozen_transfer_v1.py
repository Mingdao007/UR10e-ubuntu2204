"""FT-v1 frozen/adaptive comparison with matched nominal recovery and invariants."""
import argparse,gc,hashlib,math,shutil
from pathlib import Path
import numpy as np
from run_contact_yield import read,write
from report_yield_observer_transfer_v1 import invariant,load
from contact_yield_replay import differences
from contact_yield_metrics import compare_pair
ROOT=Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 rows=read(a.input/'summary.json')['runs'];assert len(rows)==6
 ot=read(ROOT/'report/yield-observer-transfer-v1/results.json')['rows'];old=[r for r in ot if r['scenario'] in ('nominal','sustained_release_tangent')];assert len(old)==12
 for row in old:assert hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()==row['sha256']
 compare={(r['method'],r['scenario']):r for r in old if r['observer']=='NO-v3'}
 checks=[];new=[]
 for method in ('SFC','DSFC','MSFC'):
  nominal=load(next(r for r in rows if r['method']==method and r['scenario']=='nominal'))
  for row in [r for r in rows if r['method']==method]:
   r=nominal if row['scenario']=='nominal' else load(row);baseline=load(compare[method,row['scenario']])
   delta=differences(invariant(r),invariant(baseline),atol=1e-12)
   initial=r['initial_controller_snapshot']['normal_estimate']['inward_normal_base'];frozen=all(np.array_equal(x['normal_estimate'],initial) for x in r['rows'])
   checks.append({'method':method,'scenario':row['scenario'],'matched_except_estimator_parameters':not delta,'differences':delta,'normal_state_exactly_frozen':frozen})
   item={**row,'observer':'frozen'}
   if row['scenario']!='nominal':item['pair']=compare_pair(nominal,r)
   new.append(item);del baseline
   if r is not nominal:del r
   gc.collect()
  del nominal;gc.collect()
 results=old+new;write(a.output/'results.json',{'rows':results,'checks':checks,'dataset_role':'development','live_executed':False})
 lines=['# FT-v1: frozen-observer ablation across three control dynamics','','Four new SFC/MSFC trials plus two reused DSFC trials, with the 12 legacy/NO-v3 references from OT-v1. No parameter tuning; the frozen observer is not an unknown-surface solution.','','| Method | Observer | Scenario | Failed | Force MAE N | Peak N | Path RMS mm | Progress | Load <1 N s | Yield mm | Recovery s |','|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
 fmt=lambda v,s=1:'NA' if v is None else f'{v*s:.3f}'
 for row in sorted(results,key=lambda r:(r['method'],r['scenario'],r['observer'])):
  m=row['metrics'];q=row.get('pair',{});rec='censored' if q.get('right_censored') else fmt(q.get('recovery_s'))
  lines.append('| '+' | '.join([row['method'],row['observer'],row['scenario'],str(m['failed']),fmt(m['force_mae_n']),fmt(m['force_peak_n']),fmt(m['path_rmse_m'],1000),fmt(m['progress_ratio']),fmt(m['contact_loss_duration_s']),fmt(q.get('yield_peak_m'),1000),rec])+' |')
 (a.output/'results.md').write_text('\n'.join(lines)+'\n')
 for name in ('protocol.json','manifest.json','start.json'):shutil.copy2(a.input/name,a.output/name)
 assert all(x['matched_except_estimator_parameters'] and x['normal_state_exactly_frozen'] for x in checks),checks
if __name__=='__main__':main()
