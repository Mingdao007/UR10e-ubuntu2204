"""Main's sequential launcher for the frozen offline campaign; never auto-retries."""
import datetime,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUTPUT=ROOT/'runs/yield-fair-training-v1'
LOG=OUTPUT/'launch-events.jsonl'
def invoke(command,*args):
 process=subprocess.run([sys.executable,str(ROOT/'tools/yield_fair_campaign.py'),command,'--output',str(OUTPUT),*args],cwd=ROOT,capture_output=True,text=True)
 event={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'command':command,'args':args,'exit_code':process.returncode,'stdout':process.stdout,'stderr':process.stderr}
 with LOG.open('a') as f:f.write(json.dumps(event)+'\n')
 if process.returncode:
  print(json.dumps(event),flush=True)
  raise RuntimeError('campaign command failed; no retry, inspect inflight')
 return json.loads(process.stdout)
stopped=set()
for unit in range(24):
 for method in ('SFC','DSFC','MSFC'):
  if method in stopped:continue
  status=invoke('status')['methods'][method]
  if status['stopped_before_ei']:
   stopped.add(method);print(json.dumps({'method':method,'stopped_before_ei':True,'unit':unit}),flush=True);continue
  for condition in ('nominal','disturbed'):
   result=invoke('run-next','--method',method)
   assert result['unit_index']==unit and result['condition']==condition,result
   print(json.dumps(result),flush=True)
final=invoke('status')
if not stopped:
 final['freeze']=invoke('freeze')
else:
 final['campaign_outcome']='initial feasibility stop; no full-budget freeze or superiority claim'
(OUTPUT/'launcher-result.json').write_text(json.dumps(final,indent=2)+'\n')
print(json.dumps(final),flush=True)
