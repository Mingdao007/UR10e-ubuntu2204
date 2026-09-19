"""Complement state/task verification with exact observer-parameter comparison."""
import argparse,gc,hashlib
from pathlib import Path
from run_contact_yield import read,write
from contact_yield_replay import differences
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
rows=read(a.report/'results.json')['rows'];mild={};checks=[]
for row in rows:
    path=Path(row['path']);assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
    r=read(path);params=dict(r['identity_payload']['estimator_parameters'])
    initial=params.pop('initial_inward_normal_base',None)
    if initial is not None:assert not differences(initial,r['initial_controller_snapshot']['normal_estimate']['inward_normal_base'],atol=1e-12)
    key=(row['material'],row['variant'])
    if row['surface']=='mild retained':mild[key]=params
    else:
        assert params==mild[key]
        checks.append({'material':key[0],'variant':key[1],'parameters':params,'matched':True})
    del r;gc.collect()
assert len(checks)==4
write(a.report/'estimator-bindings.json',{'passed':True,'pairs':checks,
    'initial_state_note':'Explicit constructor prior is compared through actual initial state, already checked separately; all remaining parameters match exactly.'})
