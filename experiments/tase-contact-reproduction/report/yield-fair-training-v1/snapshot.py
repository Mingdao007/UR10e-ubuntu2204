"""Read-only ledger snapshot; never executes or changes training decisions."""
import datetime,json,sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
RUN=ROOT/'runs/yield-fair-training-v1'
connection=sqlite3.connect(f'file:{RUN / "campaign.sqlite"}?mode=ro',uri=True)
connection.execute('BEGIN')
rows=connection.execute('SELECT id,controller,unit,condition,status,evidence FROM attempts ORDER BY rowid').fetchall()
connection.close()
methods={name:{'registered_units':0,'completed_pairs':0,'feasible_pairs':0,'failed_pairs':0,'pairs':[]} for name in ('SFC','DSFC','MSFC')}
units={};inflight=[]
for attempt,method,unit,condition,status,evidence in rows:
 data=json.loads(evidence) if evidence else {}
 units.setdefault((method,unit),{})[condition]={'attempt_id':attempt,'status':status,'evidence':data}
 if status=='running':inflight.append(attempt)
for (method,unit),pair in sorted(units.items()):
 summary=methods[method];summary['registered_units']+=1
 terminal=len(pair)==2 and all(x['status']!='running' for x in pair.values())
 entry={'unit':unit,'terminal':terminal,'members':{k:v['status'] for k,v in pair.items()}}
 if terminal:
  summary['completed_pairs']+=1
  successful=all(x['status']=='complete' for x in pair.values())
  summary['failed_pairs']+=not successful
  nominal=pair['nominal']['evidence'];disturbed=pair['disturbed']['evidence']
  feasible=successful and nominal.get('nominal_feasible') is True and disturbed.get('pair_feasible') is True
  summary['feasible_pairs']+=feasible
  entry.update(pair_feasible=feasible,nominal_feasible=nominal.get('nominal_feasible'),objective=disturbed.get('objective'),objective_components=disturbed.get('objective_components'),nominal_checks=nominal.get('nominal_checks'),disturbed_checks=disturbed.get('disturbed_checks'))
 summary['pairs'].append(entry)
result={'observed_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source':'transactionally consistent read-only SQLite snapshot','inflight':inflight,'methods':methods,'selection_changed':False,'validation_executed':False,'physical_executed':False}
path=Path(__file__).with_name('progress.json');path.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:{a:b for a,b in v.items() if a!='pairs'} for k,v in methods.items()}))
print('inflight',inflight)
