"""Main sequential continuation of the fixed diagnostic; no retries or reselection."""
import datetime,json,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[2]
output=root/'runs/yield-mechanism-development-v1'
manifest=json.loads((output/'manifest.json').read_text())['slots']
assert len(manifest)==60
assert not (output/'inflight.json').exists()
for slot in manifest:
 terminal=output/'attempts'/slot['slot_id']/'outcome.json'
 if terminal.exists():
  assert slot['index']<2,'unexpected existing later attempt; inspect before continuing'
  continue
 proc=subprocess.run([sys.executable,str(root/'tools/yield_mechanism_development.py'),'run-next','--output',str(output)],cwd=root,capture_output=True,text=True)
 event={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'slot_id':slot['slot_id'],'exit_code':proc.returncode,'stdout':proc.stdout,'stderr':proc.stderr}
 with (output/'launch-events.jsonl').open('a') as f:f.write(json.dumps(event)+'\n')
 if proc.returncode:raise RuntimeError('diagnostic command failed; inspect retained inflight; no retry')
 result=json.loads(proc.stdout);assert result['slot_id']==slot['slot_id']
 print(json.dumps({k:result.get(k) for k in ['slot_id','status','pair_feasible','objective','reason']}),flush=True)
(output/'launcher-result.json').write_text(json.dumps({'terminal':True,'scheduled_slots':60,'completed_commands':58,'retries':0,'scope':'development only'})+'\n')
