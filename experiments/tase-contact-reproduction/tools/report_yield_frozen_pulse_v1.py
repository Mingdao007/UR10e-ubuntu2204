"""FP-v1 matched pulse recovery and fixed event-window diagnostics."""
import argparse,gc,hashlib,shutil
from pathlib import Path
import numpy as np
from run_contact_yield import read,write
from report_yield_observer_transfer_v1 import load,invariant
from contact_yield_replay import differences
from contact_yield_metrics import compare_pair

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 protocol=read(a.input/'protocol.json');rows=read(a.input/'summary.json')['runs'];results=[];checks=[]
 for row in rows:
  r=load(row);nom=load(protocol['nominal'][row['variant']]);diff=differences(invariant(r),invariant(nom),atol=1e-12)
  diff+=differences(r['identity_payload']['estimator_parameters'],nom['identity_payload']['estimator_parameters'],'estimator_parameters',atol=1e-12)
  pair=compare_pair(nom,r)
  if r['metrics']['failed'] or nom['metrics']['failed']:
   checks.append({'variant':row['variant'],'matched_nominal':not diff,'differences':diff,'normal_state_exactly_frozen':False,'failed_pair_retained':True})
   results.append({**row,'pair':pair,'event':{k:None for k in ('force_mae_n','force_peak_n','min_force_n','post_release_max_displacement_m','post_release_displacement_integral_m_s')}})
   del r,nom;gc.collect();continue
  t=np.asarray([x['time_s'] for x in r['rows']]);pos=np.asarray([x['position_m'] for x in r['rows']]);base=np.asarray([x['position_m'] for x in nom['rows']]);load_n=np.asarray([x['true_normal_load_n'] for x in r['rows']]);distance=np.linalg.norm(pos-base,axis=1)
  window=(t>=20)&(t<23);post=t>=20.5
  initial=r['initial_controller_snapshot']['normal_estimate']['inward_normal_base'];frozen=all(np.array_equal(x['normal_estimate'],initial) for x in r['rows'])
  checks.append({'variant':row['variant'],'matched_nominal':not diff,'differences':diff,'normal_state_exactly_frozen':frozen})
  event={'window_s':[20,23],'force_mae_n':float(np.mean(abs(load_n[window]-5))),'force_peak_n':float(max(load_n[window])),
   'min_force_n':float(min(load_n[window])),'post_release_max_displacement_m':float(max(distance[post])),'post_release_displacement_integral_m_s':float(np.sum(distance[post])*r['dt_s'])}
  result={**row,'pair':pair,'event':event}
  if row['method']=='MSFC':
   eigen=np.asarray([x['controller_snapshot']['law22']['values'][19:22] for x in r['records']]);result['metric_min_eigenvalue']=float(eigen.min());result['metric_max_eigenvalue']=float(eigen.max())
  results.append(result);del r,nom;gc.collect()
 write(a.output/'results.json',{'runs':results,'checks':checks,'dataset_role':'development','live_executed':False})
 lines=['# FP-v1: fixed half-second oblique pulse, frozen observer','','Full-period metrics remain in results.json. The supplementary event window [20,23) s was fixed before runs; recovery uses the original matched nominal definition after pulse end at 20.5 s. No tuning, holdout or physical claim.','','| Variant | Failed | Full force MAE N | Event force MAE N | Event peak N | Event min N | Yield mm | Post-release max mm | Recoil mm | Recovery s |','|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
 fmt=lambda v,s=1:'NA' if v is None else f'{v*s:.6f}'
 for row in results:
  m=row['metrics'];e=row['event'];q=row['pair'];rec='censored' if q.get('right_censored') else fmt(q.get('recovery_s'))
  lines.append('| '+' | '.join([row['variant'],str(m['failed']),fmt(m['force_mae_n']),fmt(e['force_mae_n']),fmt(e['force_peak_n']),fmt(e['min_force_n']),fmt(q.get('yield_peak_m'),1000),fmt(e['post_release_max_displacement_m'],1000),fmt(q.get('recoil_m'),1000),rec])+' |')
 (a.output/'results.md').write_text('\n'.join(lines)+'\n')
 for name in ('protocol.json','start.json','manifest.json'):shutil.copy2(a.input/name,a.output/name)
 assert all(c['matched_nominal'] and c['normal_state_exactly_frozen'] for c in checks),checks
if __name__=='__main__':main()
