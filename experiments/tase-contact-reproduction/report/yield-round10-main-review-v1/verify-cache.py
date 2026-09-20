import importlib.util,json,hashlib,datetime,csv
from pathlib import Path
import numpy as np
root=Path.cwd(); script=root/'report/yield-fair-training-v1/discussion-round10/scripts/extract_rows_v10.py'
spec=importlib.util.spec_from_file_location('round10_extract',script);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.OUT=Path('/tmp/yfp10-main');m.OUT.mkdir(exist_ok=True)
entries={e['attempt_id']:e for e in csv.DictReader(m.MANIFEST.open())}
ids=[f'{method}-{u:02d}-{condition}' for u in range(5) for method in ('SFC','DSFC','MSFC') for condition in ('nominal','disturbed')]+[f'{method}-08-{condition}' for method in ('DSFC','MSFC') for condition in ('nominal','disturbed')]
verified=[]
for aid in ids:
 m.extract(entries[aid])
 with np.load(m.OUT/f'{aid}.npz') as a,np.load(Path('/tmp/yfp10')/f'{aid}.npz') as b:
  assert set(a.files)==set(b.files),aid
  assert all(np.array_equal(a[k],b[k],equal_nan=True) for k in a.files),aid
 ma=json.loads((m.OUT/f'{aid}.meta.json').read_text());mb=json.loads((Path('/tmp/yfp10')/f'{aid}.meta.json').read_text())
 ma['_extract'].pop('seconds');mb['_extract'].pop('seconds');assert ma==mb,aid
 verified.append({'id':aid,'artifact_sha256':ma['_extract']['artifact_sha256'],'arrays_equal':True,'metadata_equal_excluding_elapsed':True})
result={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'Main rerun of supplied extractor from 34 manifest-listed raw artifacts; hash verification and exact cache comparison. Not an independent extractor implementation. No simulations or ledger writes.','extractor_sha256':hashlib.sha256(script.read_bytes()).hexdigest(),'verified':verified}
Path('/tmp/yield-round10-main-cache-verification.json').write_text(json.dumps(result,indent=2)+'\n')
print('VERIFIED',len(verified))
