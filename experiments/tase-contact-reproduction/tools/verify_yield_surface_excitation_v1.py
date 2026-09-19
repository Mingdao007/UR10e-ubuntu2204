"""Check reused comparators and full replay for evaluator-only curvature variation."""
import argparse,hashlib,json,gc,copy
from pathlib import Path
from run_contact_yield import read,write
from contact_yield_replay import replay_artifact,differences

def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    rows=read(a.report/'results.json')['rows'];mild={};checks=[];replay=None
    for row in rows:
        path=Path(row['path']);assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
        r=read(path);initial=copy.deepcopy(r['initial_controller_snapshot']);initial.pop('identity')
        initial_plant=copy.deepcopy(r['initial_simulator_snapshot']);initial_plant.pop('identity')
        plant_identity=copy.deepcopy(r['plant_identity_payload']);surface=plant_identity.pop('surface')
        invariant={'parameters':r['identity_payload']['parameters'],'settings':r['identity_payload']['settings'],
            'reference_hash':digest([x['reference'] for x in r['records']]),'initial_controller':initial,
            'initial_plant':initial_plant,'plant_without_surface':plant_identity,
            'surface_origin':surface['origin_m'],'surface_sine_amp_m':surface['sine_amp_m'],
            'surface_sine_freq_per_m':surface['sine_freq_per_m']}
        key=(row['material'],row['variant'])
        if row['surface']=='mild retained':mild[key]=invariant
        else:
            diff=differences(invariant,mild[key],atol=1e-12)
            checks.append({'material':key[0],'variant':key[1],'matched_except_curvature':not diff,'differences':diff,
                'new_receipt_sha256':row['sha256']})
            if key==('compliant_high_mu','frozen_prior'):
                replay=replay_artifact(r);replay['receipt_sha256']=row['sha256']
        del r;gc.collect()
    result={'comparators':checks,'full_replay':replay,'passed':all(x['matched_except_curvature'] for x in checks) and replay is not None and replay['passed'],
        'comparison_atol':1e-12,'claim':'parameter/task/initial-state check and same-implementation replay; not independent physics validation'}
    write(a.report/'verification.json',result)
    assert result['passed'],result
if __name__=='__main__':main()
