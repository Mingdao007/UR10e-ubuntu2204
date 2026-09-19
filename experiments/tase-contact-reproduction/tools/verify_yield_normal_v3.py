"""Verify NO-v3 matched invariants and full-state replay; not physics validation."""
import argparse,copy,gc,hashlib,json
from pathlib import Path
from run_contact_yield import read,write
from contact_yield_replay import differences,replay_artifact

def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def invariant(r):
    controller=copy.deepcopy(r['initial_controller_snapshot']);controller.pop('identity');controller.pop('normal_estimate')
    plant=copy.deepcopy(r['initial_simulator_snapshot']);plant.pop('identity')
    return {'law_parameters':r['identity_payload']['parameters'],'settings':r['identity_payload']['settings'],
        'plant_identity':r['plant_identity_payload'],'initial_plant':plant,'controller_except_estimator':controller,
        'references':digest([x['reference'] for x in r['records']]),'dt_s':r['dt_s'],'preparation':r['preparation']}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    rows=read(a.report/'results.json')['rows'];snapshots={};checks=[];replay=None
    for row in rows:
        path=Path(row['path']);assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
        r=read(path);snapshots[row['id']]=invariant(r)
        if row['id']=='combined-mild-across':
            replay=replay_artifact(r);replay['receipt_sha256']=row['sha256']
        del r;gc.collect()
    for current,retained in [('combined-mild-approach','retained-approach'),('combined-mild-along','retained-along_10deg'),
        ('combined-mild-across','retained-across_10deg'),('combined-strong-approach','retained-strong-frozen'),
        ('combined-normal-hold','frozen-normal-hold'),('combined-tangent-hold','frozen-tangent-hold'),
        ('combined-mild-across','motion-only-mild-across')]:
        diff=differences(snapshots[current],snapshots[retained],atol=1e-12)
        checks.append({'pair':[current,retained],'matched_except_estimator':not diff,'differences':diff})
    result={'checks':checks,'full_state_replay':replay,'passed':all(x['matched_except_estimator'] for x in checks) and replay is not None and replay['passed'],
        'claim':'mechanical/reference/initial-state invariants except estimator, and same-implementation full-state replay; not independent physics validation'}
    write(a.report/'verification.json',result);assert result['passed'],result
if __name__=='__main__':main()
