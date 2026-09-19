"""Resume the frozen full-cycle matrix; independent simulations may run in parallel.

Existing results must match the current source and selected parameters. They are
never overwritten. Partial writes remain under a pending name for inspection.
Parallel wall times are diagnostic only, never realtime qualification.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
from pathlib import Path

from contact_yield_runner import run_closed_loop
from contact_yield_protocol import PERIOD_S
from contact_yield_metrics import compare_pair
from run_contact_yield import read, write


def job(spec):
    root, method, material, scenario, parameters = spec
    path = root / f'full-{method}-{material}-{scenario}.json.gz'
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in Path(__file__).parent.glob('contact_yield_*.py')}
    if path.exists():
        result = read(path)
        if (result['source_hashes'] != hashes
                or result['identity_payload']['parameters'] != parameters
                or result['method'] != method or result['material'] != material
                or result['scenario'] != scenario
                or result['duration_s'] != PERIOD_S
                or result['timeline'] != 'full_cycle'):
            raise ValueError('existing evidence differs: ' + str(path))
    else:
        result = run_closed_loop(method=method, material=material, scenario=scenario,
                                 duration_s=PERIOD_S, timeline='full_cycle',
                                 law_parameters=parameters, record_fullstate=True)
        pending = path.with_name(path.name.replace('.json.gz', '.pending.json.gz'))
        write(pending, result)
        # Hard-link publication fails if another process created the destination.
        path.hardlink_to(pending)
        pending.unlink()
    compact = {k: v for k, v in result.items() if k not in ('records',)}
    return path.name, compact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 7))
    args = parser.parse_args()
    if (args.root / 'summary.json').exists():
        parser.error('completed matrix is immutable')
    selected = read(args.root / 'screen.json')['selected']
    specs = [(args.root, method, material, scenario,
              selected['SFC' if method == 'SFC_RADIAL' else method])
             for method in ('SFC', 'SFC_RADIAL', 'DSFC', 'MSFC')
             for material in ('stiff_low_mu', 'compliant_high_mu')
             for scenario in ('nominal', 'sustained_release_normal',
                              'sustained_release_tangent', 'short_pulse_oblique')]
    summary = []
    nominal = {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for name, result in pool.map(job, specs):
            item = {k: result[k] for k in ('method', 'material', 'scenario',
                                          'metrics', 'full_cycle', 'source_hashes')}
            item['file'] = name
            key = (result['method'], result['material'])
            if result['scenario'] == 'nominal':
                nominal[key] = result
            else:
                item['pair'] = compare_pair(nominal[key], result)
            summary.append(item)
            print(name, result['metrics']['failed'], flush=True)
    write(args.root / 'summary.json', {
        'runs': summary, 'source_screen': 'screen.json',
        'formal_campaign_complete': False, 'live_executed': False,
        'resumed_with': Path(__file__).name, 'parallel_workers': args.workers,
        'timing_qualification': False})
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in args.root.iterdir() if p.is_file()}
    write(args.root / 'manifest.json', {'sha256': manifest,
          'claim_scope': 'offline mechanism screen; no formal held-out evidence'})


if __name__ == '__main__':
    main()
