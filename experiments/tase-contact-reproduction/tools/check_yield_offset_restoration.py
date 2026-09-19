"""Shared-outer-loop ablation: measured path restoring without a command-integral spring.

The same change is applied to all three methods, with frozen law parameters and
8 plant substeps. This is design/debug evidence, not a new winner or holdout.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
from pathlib import Path

import contact_yield_runner as runner
from contact_yield_controller import YieldSettings
from check_yield_plant_refinement import refined_plant
from contact_yield_protocol import PERIOD_S
from contact_yield_metrics import compare_pair
from run_contact_yield import read, write


def run(spec):
    root, method, scenario, parameters = spec
    runner.YieldSimulator = refined_plant(8)
    result = runner.run_closed_loop(method=method, scenario=scenario,
        duration_s=PERIOD_S, timeline='full_cycle', law_parameters=parameters,
        settings=YieldSettings(compliance_stiffness_n_per_m=0), record_fullstate=True)
    name = f'{method}-{scenario}.json.gz'
    write(root / name, result)
    return name, {k: v for k, v in result.items() if k != 'records'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screen', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    selected = read(args.screen)['selected']
    specs = [(args.output, m, s, selected[m]) for m in ('SFC', 'DSFC', 'MSFC')
             for s in ('nominal', 'sustained_release_tangent')]
    results = {}
    with ProcessPoolExecutor(max_workers=4) as pool:
        for name, result in pool.map(run, specs):
            results[name] = result
            print(name, result['metrics']['failed'], flush=True)
    pairs = []
    for m in ('SFC', 'DSFC', 'MSFC'):
        nominal = results[f'{m}-nominal.json.gz']
        disturbed = results[f'{m}-sustained_release_tangent.json.gz']
        pairs.append({'method': m, 'nominal': nominal['metrics'],
                      'disturbed': disturbed['metrics'],
                      'pair': compare_pair(nominal, disturbed)})
    write(args.output / 'summary.json', {'pairs': pairs,
        'shared_change': 'compliance_stiffness_n_per_m=0; measured path stiffness stays 120 N/m',
        'plant_substeps': 8, 'formal_holdout': False,
        'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    write(args.output / 'manifest.json', {'sha256': {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in args.output.iterdir() if p.is_file()}})


if __name__ == '__main__':
    main()
