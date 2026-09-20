import json,hashlib,subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[4];d=root/'report/yield-fair-tuning-v1/discussion-round7'
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
 return h.hexdigest()
a=json.loads((d/'data/round7-input-hashes.json').read_text())
checks=[]
for group in ('report_inputs','tool_inputs','raw_artifacts'):
 for e in a.get(group,[]):checks.append({'path':e['path'],'match':digest(root/e['path'])==e['sha256']})
manifest=json.loads((d/'data/round7-receipt-manifest.json').read_text())
for e in manifest['receipts']:checks.append({'path':e['path'],'match':digest(root/e['path'])==e['sha256_now']})
receipt=json.loads((d/'round7-input-receipt.json').read_text())
for e in receipt['outputs']:checks.append({'path':e['path'],'match':digest(root/e['path'])==e['sha256']})
import sys
result=subprocess.run([sys.executable,str(d/'scripts/analyze_round7.py')],cwd=root,capture_output=True,text=True,check=True)
recomputed=json.loads(result.stdout)
expected=json.loads((d/'data/round7-analysis.json').read_text())
report={'hash_count':len(checks),'all_hashes_match':all(x['match'] for x in checks),'checks':checks,'analysis_exact_match':recomputed==expected,'formal_units':0}
(d/'main-verification.json').write_text(json.dumps(report,indent=2)+'\n')
print({k:v for k,v in report.items() if k!='checks'})
assert report['all_hashes_match'] and report['analysis_exact_match']
