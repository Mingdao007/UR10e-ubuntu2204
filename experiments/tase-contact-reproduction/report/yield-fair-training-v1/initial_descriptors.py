"""Describe first sealed pair of every arm without changing training or ranking."""
import hashlib,json,math,sqlite3
from pathlib import Path
import numpy as np
from contact_yield_metrics import compare_pair
ROOT=Path(__file__).resolve().parents[2];RUN=ROOT/'runs/yield-fair-training-v1'
c=sqlite3.connect(f'file:{RUN/"campaign.sqlite"}?mode=ro',uri=True)
sealed={a:(s,json.loads(e)) for a,s,e in c.execute('SELECT id,status,evidence FROM attempts WHERE unit=0') if e};c.close()
result={'scope':'first shared initial triple only; no tuned or superiority claim','formal_units_per_method':1,'methods':{}}
for method in ('SFC','DSFC','MSFC'):
 pair=[];receipts={}
 for condition in ('nominal','disturbed'):
  attempt=f'{method}-00-{condition}';status,evidence=sealed[attempt];assert status=='complete'
  path=RUN/'attempts'/attempt/'artifact.json'
  h=hashlib.sha256()
  with path.open('rb') as stream:
   for chunk in iter(lambda:stream.read(1048576),b''):h.update(chunk)
  assert h.hexdigest()==evidence['artifact_sha256']
  a=json.loads(path.read_text());a.pop('records');pair.append(a)
  receipts[condition]={'path':str(path),'sha256':h.hexdigest()}
 nominal,disturbed=pair;recovery=compare_pair(nominal,disturbed)
 post=[r for r in disturbed['rows'] if r['time_s']>=recovery['release_s']]
 result['methods'][method]={'parameters':nominal['identity_payload']['parameters'],'nominal':nominal['metrics'],'disturbed':disturbed['metrics'],'recovery':recovery,'minimum_load_n':min(r['true_normal_load_n'] for r in disturbed['rows']),'post_release_attitude_rms_deg':math.degrees(float(np.sqrt(np.mean([r['orientation_error_rad']**2 for r in post])))),'terminal_load_difference_n':disturbed['rows'][-1]['true_normal_load_n']-nominal['rows'][-1]['true_normal_load_n'],'objective_components':sealed[f'{method}-00-disturbed'][1]['objective_components'],'receipts':receipts}
 del nominal,disturbed,a,pair
out=Path(__file__).with_name('initial-descriptors.json');out.write_text(json.dumps(result,indent=2)+'\n')
for method,row in result['methods'].items():
 print(method,{k:row[k] for k in ('minimum_load_n','post_release_attitude_rms_deg','terminal_load_difference_n')},'recovery',row['recovery']['recovery_s'])
