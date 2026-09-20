"""FM-v1 matched MSFC metric ablation; verify actual metric eigenvalues."""
import argparse,copy,gc,hashlib
from pathlib import Path
import numpy as np
from run_contact_yield import read,write
from report_yield_observer_transfer_v1 import invariant,load
from contact_yield_replay import differences
from contact_yield_metrics import compare_pair
from contact_yield_laws import YieldLaw

def stripped(r):
 law=YieldLaw(r['method'],r['identity_payload']['parameters'],dt_s=r['dt_s'])
 try:
  assert law.identity==r['identity_payload']['law_identity']
  assert law.snapshot().binding_id==r['initial_controller_snapshot']['law22']['binding_id']
 finally:law.close()
 x=invariant(r);x['identity_except_estimator']['parameters'].pop('minimum_metric_eigenvalue');x['identity_except_estimator'].pop('law_identity');x['initial_controller']['law22'].pop('binding_id');return x
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 protocol=read(a.input/'protocol.json');rows=read(a.input/'summary.json')['runs'];on={r['scenario']:r for r in protocol['retained_on']};out=[];checks=[]
 nominal=load(next(r for r in rows if r['scenario']=='nominal'));on_nominal=load(on['nominal'])
 for row in rows:
  off=nominal if row['scenario']=='nominal' else load(row);active=on_nominal if row['scenario']=='nominal' else load(on[row['scenario']])
  diff=differences(stripped(off),stripped(active),atol=1e-12);eoff=np.asarray([r['controller_snapshot']['law22']['values'][19:22] for r in off['records']]);eon=np.asarray([r['controller_snapshot']['law22']['values'][19:22] for r in active['records']])
  checks.append({'scenario':row['scenario'],'only_metric_floor_changed':not diff,'differences':diff,'identity_metric_max_deviation':float(np.max(abs(eoff-1))),
   'active_metric_min_eigenvalue':float(eon.min()),'active_metric_max_eigenvalue':float(eon.max())})
  for label,item,r in [('identity_metric',row,off),('on',on[row['scenario']],active)]:
   v={**item,'metric':label}
   if row['scenario']!='nominal':v['pair']=compare_pair(nominal if label=='identity_metric' else on_nominal,r)
   out.append(v)
  if off is not nominal:del off
  if active is not on_nominal:del active
  gc.collect()
 write(a.output/'results.json',{'runs':out,'checks':checks,'dataset_role':'development','live_executed':False})
 lines=['# FM-v1: mechanically matched MSFC metric ablation','','Only minimum_metric_eigenvalue differs. Identity-metric eigenvalues are checked directly over every stored tick; force-history/filter states are retained. This is not a claim that all memory states are disabled.','','| Scenario | Metric | Force MAE N | Peak N | Path RMS mm | Progress | Yield mm | Recovery s |','|---|---|---:|---:|---:|---:|---:|---:|']
 fmt=lambda x,s=1:'NA' if x is None else f'{x*s:.6f}'
 for row in out:
  m=row['metrics'];q=row.get('pair',{});rec='censored' if q.get('right_censored') else fmt(q.get('recovery_s'))
  lines.append('| '+' | '.join([row['scenario'],row['metric'],fmt(m['force_mae_n']),fmt(m['force_peak_n']),fmt(m['path_rmse_m'],1000),fmt(m['progress_ratio']),fmt(q.get('yield_peak_m'),1000),rec])+' |')
 (a.output/'results.md').write_text('\n'.join(lines)+'\n')
 assert all(c['only_metric_floor_changed'] and c['identity_metric_max_deviation']<1e-12 for c in checks),checks
if __name__=='__main__':main()
