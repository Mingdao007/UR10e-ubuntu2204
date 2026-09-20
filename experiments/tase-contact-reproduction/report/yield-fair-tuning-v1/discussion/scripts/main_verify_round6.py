from pathlib import Path
import json,hashlib
root=Path.cwd();d=root/'report/yield-fair-tuning-v1/discussion'
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
manifest=json.loads((d/'data/round6-receipt-manifest.json').read_text());checks=[]
for r in manifest['receipts']:
 checks.append({'path':r['path'],'matches':sha(root/r['path'])==r['sha256_now']})
inputs=json.loads((d/'data/round6-input-hashes.json').read_text());ic=[]
for group,rows in inputs.items():
 if not isinstance(rows,list):continue
 for r in rows:
  if not isinstance(r,dict) or 'path' not in r or 'sha256' not in r:continue
  ic.append({'path':r['path'],'matches_current':sha(root/r['path'])==r['sha256']})
params=json.loads((root/'report/yield-frozen-transfer-v1/protocol.json').read_text())['parameters']['MSFC'];h=1.;tau=params['tau_recovery_s'];k=params['kappa_per_n2_s'];q=-k*tau;start=q;dt=.002
for i in range(300):
 h=h/(1+dt/params['tau_force_s']);q=(q-dt*k*h*h)/(1+dt/tau)
out={'raw_checks':checks,'input_checks':ic,'zero_force_after_steady_scalar_feature':{'dt_s':dt,'elapsed_s':.6,'history_ratio':h,'structure_ratio':q/start,'claim_scope':'scalar invariant subspace of native memory recurrence, not closed-loop or global memory settling bound'},'all_raw_match':all(x['matches'] for x in checks)}
Path('/tmp/yield-round6-main-verification.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps({'raw_count':len(checks),'all_raw_match':out['all_raw_match'],'input_count':len(ic),'changed_inputs':[r for r in ic if not r['matches_current']],'memory':out['zero_force_after_steady_scalar_feature']},indent=2))
