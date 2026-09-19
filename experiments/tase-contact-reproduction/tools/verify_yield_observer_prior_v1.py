"""Verify fixed task/plant, exact frozen state and one full-state forward replay."""
from pathlib import Path
import argparse,hashlib,json,gc
from run_contact_yield import read,write
from contact_yield_replay import replay_artifact

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    rows=read(a.input/'summary.json')['runs'];baselines={};verified=[];replay=None
    for row in rows:
        path=Path(row['path']);assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
        run=read(path)
        reference_hash=digest([record['reference'] for record in run['records']])
        invariant={'initial_plant':digest(run['initial_simulator_snapshot']),
                   'plant_identity':digest(run['plant_identity_payload']),
                   'references':reference_hash,'approach':run['initial_controller_snapshot']['normal_estimate']['approach_inward_base']}
        if row['prior']=='approach':baselines[row['material']]=invariant
        else:assert invariant==baselines[row['material']],f"physical task changed: {path}"
        prior=run['initial_controller_snapshot']['normal_estimate']['inward_normal_base']
        assert all(record['controller_snapshot']['normal_estimate']['inward_normal_base']==prior for record in run['records'])
        verified.append({'material':row['material'],'prior':row['prior'],'receipt_sha256':row['sha256'],
            'records':len(run['records']),'invariants':invariant,'exact_frozen_state':True})
        if row['material']=='compliant_high_mu' and row['prior']=='across_10deg':
            replay=replay_artifact(run);assert replay['passed'];replay['receipt_sha256']=row['sha256']
        del run;gc.collect()
    assert len(verified)==6 and replay is not None
    write(a.output,{'passed':True,'verified':verified,'full_replay':replay,
        'claim':'same implementation full-state replay, not independent physical validation'})
if __name__=='__main__':main()
