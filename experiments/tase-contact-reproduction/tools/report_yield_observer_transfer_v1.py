"""OT-v1 matched observer transfer, legacy references and unchanged recovery definition."""
import argparse,hashlib,gc,copy,math
from pathlib import Path
from run_contact_yield import read,write
from contact_yield_metrics import compare_pair
from contact_yield_replay import differences
ROOT=Path(__file__).resolve().parents[1]
def load(row):
 p=Path(row['path']);assert hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256'];return read(p)
def invariant(r):
 identity=copy.deepcopy(r['identity_payload']);identity.pop('estimator_parameters')
 controller=copy.deepcopy(r['initial_controller_snapshot']);controller.pop('identity')
 return {'identity_except_estimator':identity,'plant':r['plant_identity_payload'],'initial_plant':r['initial_simulator_snapshot'],
  'initial_controller':controller,'reference':[x['reference'] for x in r['records']]}
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 new=read(a.input/'summary.json')['runs'];assert len(new)==9
 old=read(ROOT/'runs/yield-transfer-v1/summary.json')['runs'];labels={'SFC':'SFC','DSFC':'DSFC','MSFC':'MSFC-GM-v1-g50-on'}
 old={(m,r['scenario']):r for m,label in labels.items() for r in old if r['variant']==label and r['material']=='stiff_low_mu' and r['scenario'] in ('nominal','sustained_release_normal','sustained_release_tangent')};assert len(old)==9
 results=[];checks=[]
 for method in labels:
  nominal=next(row for row in new if row['method']==method and row['scenario']=='nominal');n=load(nominal)
  for row in [r for r in new if r['method']==method]:
   r=n if row['scenario']=='nominal' else load(row)
   previous=old[method,row['scenario']];legacy=load(previous)
   diff=differences(invariant(r),invariant(legacy),atol=1e-12)
   checks.append({'method':method,'scenario':row['scenario'],'matched_except_estimator_parameters':not diff,'differences':diff})
   current={**row,'observer':'NO-v3'}
   if row['scenario']!='nominal':current['pair']=compare_pair(n,r)
   results.extend([{'method':method,'observer':'legacy',**previous},current])
   del legacy
   if r is not n:del r
   gc.collect()
  del n;gc.collect()
 write(a.output/'results.json',{'rows':results,'checks':checks,'dataset_role':'development','live_executed':False})
 lines=['# OT-v1: common-observer transfer across the three fixed control dynamics','','Six new SFC/MSFC trials and three retained DSFC NO-v3 trials, compared with nine hash-verified legacy-observer trials. Mechanical/settings/initial-state/reference invariant checks are retained in results.json; failed or shortened traces remain visible and cannot support matched ranking. No retuning, equal-budget optimization, holdout or final ranking.','','| Method | Observer | Scenario | Failed | Normal RMS deg | Force MAE N | Peak N | Path RMS mm | Progress | Loss s | Recovery s |','|---|---|---|---|---:|---:|---:|---:|---:|---:|---|']
 fmt=lambda v,scale=1:'NA' if v is None else f'{v*scale:.3f}'
 for row in results:
  m=row['metrics'];pair=row.get('pair',{});rec='censored' if pair.get('right_censored') else fmt(pair.get('recovery_s'))
  lines.append('| '+' | '.join([row['method'],row['observer'],row['scenario'],str(m['failed']),fmt(m['normal_estimation_rmse_rad'],180/math.pi),fmt(m['force_mae_n']),fmt(m['force_peak_n']),fmt(m['path_rmse_m'],1000),fmt(m['progress_ratio']),fmt(m['contact_loss_duration_s']),rec])+' |')
 (a.output/'results.md').write_text('\n'.join(lines)+'\n')
 assert all(x['matched_except_estimator_parameters'] for x in checks),checks
if __name__=='__main__':main()
