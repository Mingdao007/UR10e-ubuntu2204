import importlib.util,json,hashlib,datetime
from pathlib import Path
import numpy as np
root=Path.cwd(); script=root/'report/yield-fair-training-v1/discussion-round8/scripts/extract_rows_v8.py'
spec=importlib.util.spec_from_file_location('round8_extract',script);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.OUT=Path('/tmp/yfp8-main');m.OUT.mkdir(exist_ok=True)
snap=json.loads(m.SNAP.read_text());verified=[]
for e in snap['attempts']:
 m.extract(e)
 aid=e['id'];a=np.load(m.OUT/f'{aid}.npz');b=np.load(Path('/tmp/yfp8')/f'{aid}.npz')
 assert set(a.files)==set(b.files),aid
 assert all(np.array_equal(a[k],b[k],equal_nan=True) for k in a.files),aid
 ma=json.loads((m.OUT/f'{aid}.meta.json').read_text());mb=json.loads((Path('/tmp/yfp8')/f'{aid}.meta.json').read_text())
 ma['_extract'].pop('seconds');mb['_extract'].pop('seconds');assert ma==mb,aid
 verified.append({'id':aid,'artifact_sha256':ma['_extract']['artifact_sha256'],'arrays_equal':True,'metadata_equal_excluding_elapsed':True})
result={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'Independent rerun of supplied extraction from every fixed-snapshot raw artifact; hash verification and exact cache comparison. No simulations or ledger writes.','extractor_sha256':hashlib.sha256(script.read_bytes()).hexdigest(),'verified':verified}
Path('/tmp/yield-round8-main-cache-verification.json').write_text(json.dumps(result,indent=2)+'\n')
print('VERIFIED',len(verified))
