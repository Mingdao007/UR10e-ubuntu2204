"""Fixed-control-period 4/8-substep full-cycle sensitivity on frozen parameters.

This checks nominal and sustained normal loading for the three primary methods.
No retuning, hardware or statistical holdout claim is made.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
from pathlib import Path

import contact_yield_runner as runner
from check_yield_plant_refinement import refined_plant
from contact_yield_protocol import PERIOD_S
from contact_yield_metrics import compare_pair
from contact_yield_replay import refinement_error
from run_contact_yield import read, write


def run(spec):
    root, method, scenario, substeps, parameters = spec
    runner.YieldSimulator = refined_plant(substeps)
    artifact = runner.run_closed_loop(method=method, scenario=scenario,
        duration_s=PERIOD_S, timeline='full_cycle', dt_s=.002,
        law_parameters=parameters, record_fullstate=True)
    name = f'{method}-{scenario}-plant{substeps}.json.gz'
    write(root / name, artifact)
    return name, {k: v for k, v in artifact.items() if k != 'records'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screen', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 7))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    selected = read(args.screen)['selected']
    specs = [(args.output, method, scenario, n, selected[method])
             for method in ('SFC', 'DSFC', 'MSFC')
             for scenario in ('nominal', 'sustained_release_normal')
             for n in (4, 8)]
    results = {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for name, result in pool.map(run, specs):
            results[name] = result
            print(name, result['metrics']['failed'], flush=True)
    comparisons = []
    pairs = []
    for method in ('SFC', 'DSFC', 'MSFC'):
        for scenario in ('nominal', 'sustained_release_normal'):
            coarse = results[f'{method}-{scenario}-plant4.json.gz']
            fine = results[f'{method}-{scenario}-plant8.json.gz']
            comparisons.append({'method': method, 'scenario': scenario,
                'coarse_metrics': coarse['metrics'], 'fine_metrics': fine['metrics'],
                'difference': refinement_error(coarse['rows'], fine['rows'],
                                               coarse_dt_s=.002, fine_dt_s=.002)})
        for n in (4, 8):
            pairs.append({'method': method, 'plant_substeps': n,
                'pair': compare_pair(results[f'{method}-nominal-plant{n}.json.gz'],
                                     results[f'{method}-sustained_release_normal-plant{n}.json.gz'])})
    write(args.output / 'summary.json', {'comparisons': comparisons, 'pairs': pairs,
        'claim_scope': 'fixed 2ms controller; plant 0.5/0.25ms; full cycle; no retuning or hardware',
        'controller_refinement': False, 'formal_holdout': False,
        'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    write(args.output / 'manifest.json', {'sha256': {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in args.output.iterdir() if p.is_file()}})


if __name__ == '__main__':
    main()
