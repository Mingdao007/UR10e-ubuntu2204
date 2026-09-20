import csv,datetime,hashlib,json
from pathlib import Path
root=Path.cwd();dev=root/'runs/yield-mechanism-development-v1';train=root/'runs/yield-fair-training-v1'
manifest={r['attempt_id']:r for r in csv.DictReader((root/'report/yield-training-report-final-v1/artifact_digest_manifest.csv').open())}
fields=('rows','records','metrics','initial_controller_snapshot','initial_simulator_snapshot','formal_initial_snapshot','final_controller_snapshot','final_simulator_snapshot')
def filehash(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
def scientific(p,expected):
 digest=filehash(p);assert digest==expected,(str(p),digest,expected)
 a=json.loads(p.read_text())
 campaign_label=a['metrics'].pop('campaign_kind')
 return digest,campaign_label,{k:hashlib.sha256(json.dumps(a[k],sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest() for k in fields}
results=[]
for condition in ('nominal','disturbed'):
 sid=f'u0-SFC-base-{condition}';aid=f'SFC-00-{condition}';op=dev/'attempts'/sid/'outcome.json';o=json.loads(op.read_text());assert o['status']=='complete'
 dh,dl,d=scientific(op.parent/'artifact.json',o['artifact_sha256']);t=manifest[aid];th,tl,v=scientific(train/t['path'],t['artifact_sha256'])
 equal={k:d[k]==v[k] for k in fields};assert all(equal.values()),(sid,equal)
 results.append({'slot':sid,'training_attempt':aid,'development_artifact_sha256':dh,'training_artifact_sha256':th,'excluded_metadata_difference':{'metrics.campaign_kind':{'development':dl,'training':tl}},'fields_equal_after_explicit_label_exclusion':equal,'scientific_field_sha256':d})
result={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'Exact scientific-field reproduction of completed unit0 SFC base pair. Both raw files hash-verified before parsing; metrics.campaign_kind is explicitly excluded (diagnostic_seed versus training); runtime timing and campaign identity are not asserted identical. No simulation, no retuning, no validation use.','results':results}
Path('/tmp/yield-development-base-reproduction.json').write_text(json.dumps(result,indent=2)+'\n');print('EXACT',len(results),'artifacts',len(fields),'fields each')
