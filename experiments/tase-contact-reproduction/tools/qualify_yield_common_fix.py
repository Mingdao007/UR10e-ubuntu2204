"""Check the corrected shared outer loop at fixed control period, without retuning."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
from pathlib import Path

from contact_yield_runner import run_closed_loop
from contact_yield_protocol import PERIOD_S
from contact_yield_replay import refinement_error
from run_contact_yield import read, write


def job(spec):
    output, method, scenario, n, parameters = spec
    r = run_closed_loop(method=method, scenario=scenario, duration_s=PERIOD_S,
                        timeline='full_cycle', plant_substeps=n,
                        law_parameters=parameters, record_fullstate=True)
    name = f'{method}-{scenario}-plant{n}.json.gz'
    write(output/name, r)
    return name, {k: v for k, v in r.items() if k != 'records'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--screen', type=Path, required=True)
    p.add_argument('--offset-study', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    parameters = read(a.screen)['selected']
    specs = [(a.output, m, s, 4, parameters[m])
             for m in ('SFC', 'DSFC', 'MSFC')
             for s in ('nominal', 'sustained_release_tangent')]
    specs += [(a.output, 'MSFC', 'sustained_release_normal', n, parameters['MSFC']) for n in (4, 8)]
    results = {}
    with ProcessPoolExecutor(max_workers=4) as pool:
        for name, result in pool.map(job, specs):
            results[name] = result
            print(name, result['metrics']['failed'], flush=True)
    comparisons = []
    for m in ('SFC', 'DSFC', 'MSFC'):
        scenarios = ['nominal', 'sustained_release_tangent']
        if m == 'MSFC':
            scenarios.append('sustained_release_normal')
        for s in scenarios:
            coarse = results[f'{m}-{s}-plant4.json.gz']
            if s == 'sustained_release_normal':
                fine = results[f'{m}-{s}-plant8.json.gz']
            else:
                fine = read(a.offset_study/f'{m}-{s}.json.gz')
            if coarse['identity'] != fine['identity']:
                raise ValueError('controller or shared settings changed across refinement')
            if coarse['metrics']['failed'] or fine['metrics']['failed']:
                raise ValueError('refinement member failed; no successful comparison')
            comparisons.append({'method': m, 'scenario': s,
                'coarse_metrics': coarse['metrics'], 'fine_metrics': fine['metrics'],
                'difference': refinement_error(coarse['rows'], fine['rows'],
                                               coarse_dt_s=.002, fine_dt_s=.002)})
    write(a.output/'summary.json', {'comparisons': comparisons,
          'controller_period_s': .002, 'plant_substeps': [4, 8],
          'command_integral_spring_n_per_m': 0, 'formal_holdout': False,
          'claim_scope': 'corrected shared-loop integration sensitivity; no robot evidence',
          'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    write(a.output/'manifest.json', {'sha256': {
        f.name: hashlib.sha256(f.read_bytes()).hexdigest()
        for f in a.output.iterdir() if f.is_file()}})


if __name__ == '__main__':
    main()
