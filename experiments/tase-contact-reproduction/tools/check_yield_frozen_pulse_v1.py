"""FP-v1: matched metric ablation, full replay and paired 1 ms refinement."""
import argparse
import gc
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from run_contact_yield import read, write
from report_yield_observer_transfer_v1 import load, invariant
from report_yield_frozen_memory_v1 import stripped
from contact_yield_replay import differences, replay_artifact, refinement_error
from contact_yield_runner import run_closed_loop
from contact_yield_controller import YieldSettings
from contact_yield_metrics import compare_pair
from study_yield_observer_transfer_v1 import sha


def check(spec):
    output, variant, row = spec
    original = load(row)
    ident = original['identity_payload']
    replay = replay_artifact(original) if row['scenario'] != 'nominal' else None
    fine = run_closed_loop(
        method=original['method'], scenario=original['scenario'],
        material=original['material'], duration_s=original['duration_s'],
        dt_s=.001, timeline=original['timeline'], preparation=original['preparation'],
        law_parameters=ident['parameters'], settings=YieldSettings(**ident['settings']),
        qp_library=ident['qp_library'], estimator_parameters=ident['estimator_parameters'],
        surface_parameters=original.get('surface_parameters'), plant_substeps=8,
        require_ur10e=original['kinematics_kind']=='ur10e_calibrated_pinocchio')
    path = output / variant / (row['scenario'] + '.json.gz')
    write(path, fine)
    result = {'variant': variant, 'scenario': row['scenario'], 'path': str(path.resolve()),
              'sha256': sha(path), 'metrics': fine['metrics'], 'replay_coarse': replay}
    if not fine['metrics']['failed']:
        result['refinement'] = refinement_error(original['rows'], fine['rows'],
                                                coarse_dt_s=.002, fine_dt_s=.001)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(); a.output.mkdir(parents=True, exist_ok=False)
    protocol = read(a.input/'protocol.json')
    for name, digest in protocol['source_hashes'].items():
        assert sha(Path(__file__).parent/name) == digest, name
    rows = read(a.input/'summary.json')['runs']
    pulse = {r['variant']: r for r in rows if r['method']=='MSFC'}
    on = load(pulse['MSFC']); off = load(pulse['MSFC-identity'])
    diff = differences(stripped(on), stripped(off), atol=1e-12)
    assert not diff, diff
    eigen = [x for r in off['records'] for x in r['controller_snapshot']['law22']['values'][19:22]]
    assert max(abs(x-1) for x in eigen) < 1e-12
    del on, off; gc.collect()
    jobs = [(a.output, v, r) for v in pulse for r in (protocol['nominal'][v], pulse[v])]
    write(a.output/'protocol.json', {'version':'FP-v1-refinement', 'dataset_role':'development',
        'inputs':[{'variant':v, **r} for _,v,r in jobs],
        'only_metric_floor_changed':True, 'identity_metric_exact':True,
        'dt_s':.001, 'plant_substeps':8, 'source_hashes':protocol['source_hashes'],
        'claim_scope':'Both controller and plant step halved; matched fine nominals. No convergence order or physical qualification.'})
    manifest = {'contract_id':'ur10e_concurrency_contract_v1', 'task':'FP-v1 paired refinement',
        'dependencies':[str((a.input/'manifest.json').resolve())], 'resource_lane':'CPU throughput',
        'claim_class':'offline development diagnostic', 'workers':2,
        'started_at':datetime.now(timezone.utc).isoformat(), 'exit_code':None,
        'output_paths':[str(a.output.resolve())], 'live_executed':False}
    write(a.output/'start.json', manifest)
    try:
        with ProcessPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(check, jobs))
        pairs = []
        for v in pulse:
            nominal = load(next(r for r in results if r['variant']==v and r['scenario']=='nominal'))
            disturbed = load(next(r for r in results if r['variant']==v and r['scenario']!='nominal'))
            diff = differences(invariant(nominal), invariant(disturbed), atol=1e-12)
            pairs.append({'variant':v, 'matched_nominal':not diff, 'differences':diff,
                          'pair':compare_pair(nominal, disturbed)})
            del nominal, disturbed; gc.collect()
        write(a.output/'results.json', {'runs':results, 'pairs':pairs, 'dataset_role':'development'})
        assert all(not r['metrics']['failed'] for r in results)
        assert all(r['replay_coarse'] is None or r['replay_coarse']['passed'] for r in results)
        assert all(r['matched_nominal'] for r in pairs)
        manifest['exit_code']=0
    except BaseException:
        manifest['exit_code']=1
        raise
    finally:
        manifest['finished_at']=datetime.now(timezone.utc).isoformat()
        write(a.output/'parallel_run_manifest.json', manifest)


if __name__=='__main__':
    main()
