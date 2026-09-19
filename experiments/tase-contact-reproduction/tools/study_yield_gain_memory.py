"""GM-v1: bounded development-only gain x memory-mechanical-coupling ablation.

Beta=1 makes A=I exactly, while retaining and recording the evolving h/S state.
It is not a reset-memory experiment and not a replacement for the SFC baseline.
No formal tuning budget or final holdout is consumed by this diagnostic.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
from pathlib import Path

import numpy as np
from contact_yield_runner import run_closed_loop
from contact_yield_protocol import PERIOD_S
from contact_yield_metrics import compare_pair
from contact_yield_replay import refinement_error
from run_contact_yield import read, write


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def tail_diagnostic(rows):
    tail = [r for r in rows if r['time_s'] >= PERIOD_S-10]
    signal = np.array([r['true_normal_load_n'] for r in tail])
    centered = signal-signal.mean()
    spectrum = abs(np.fft.rfft(centered))**2
    freq = np.fft.rfftfreq(len(signal), .002)
    peak = 1+int(np.argmax(spectrum[1:]))
    return {'window': 'final 10s after release', 'load_mean_n': float(signal.mean()),
            'load_std_n': float(signal.std()),
            'dominant_frequency_hz': float(freq[peak]),
            'claim': 'sampled spectral diagnostic; not proof of a limit cycle'}


def run(spec):
    out, label, params, scenario, substeps, reuse = spec
    if reuse is not None:
        result = read(reuse)
        if (result['identity_payload']['parameters'] != params
                or result['identity_payload']['settings']['compliance_stiffness_n_per_m'] != 0
                or result['plant_identity_payload']['integration_substeps'] != substeps
                or result['scenario'] != scenario or not result['full_cycle']):
            raise ValueError('retained baseline does not match frozen factorial cell')
        path = reuse
    else:
        result = run_closed_loop(method='MSFC', scenario=scenario,
            duration_s=PERIOD_S, timeline='full_cycle', law_parameters=params,
            plant_substeps=substeps, record_fullstate=True)
        path = out/f'{label}-{scenario}-plant{substeps}.json.gz'
        write(path, result)
    return label, substeps, scenario, str(path.resolve()), {
        k: v for k, v in result.items() if k != 'records'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    original = read(ROOT/'config/contact_yield_candidates/msfc.json')
    specs = []
    variants = {}
    for gain in (1., .5):
        for memory in ('on', 'identity_metric'):
            label = f'MSFC-GM-v1-g{int(gain*100)}-{memory}'
            params = {**original, 'g': original['g']*gain}
            if memory == 'identity_metric':
                params['minimum_metric_eigenvalue'] = 1.
            variants[label] = params
            for scenario in ('nominal', 'sustained_release_normal'):
                for n in (4, 8):
                    reuse = None
                    if gain == 1 and memory == 'on':
                        if scenario == 'nominal' and n == 8:
                            reuse = ROOT/'runs/yield-offset-ablation/MSFC-nominal.json.gz'
                        else:
                            reuse = ROOT/'runs/yield-common-fix-qualification'/f'MSFC-{scenario}-plant{n}.json.gz'
                    specs.append((args.output, label, params, scenario, n, reuse))
    write(args.output/'protocol.json', {
        'version': 'GM-v1', 'dataset_role': 'development_only', 'variants': variants,
        'common_outer': 'Kp=120 N/m, Kz=0, force filter=20ms; all other settings unchanged',
        'control_dt_s': .002, 'plant_substeps': [4, 8],
        'input_frame': 'base; force residual plus measured tangent path restoring',
        'native_equations': 'existing smysfc_step, full 22-slot state, implicit mechanical update; no reset',
        'hypotheses': ['output gain and delay may cause sensitivity without memory coupling',
                       'memory coupling may introduce additional sensitivity at fixed gain'],
        'new_run_count': 12, 'retained_baseline_count': 4,
        'retain_all_cells': True, 'formal_tuning_budget_used': False,
        'source_hashes': {p.name: digest(p) for p in Path(__file__).parent.glob('contact_yield_*.py')},
        'harness_sha256': digest(Path(__file__)),
        'retained_inputs': {str(s[-1].resolve()): digest(s[-1]) for s in specs if s[-1] is not None}})
    results = {}
    receipts = []
    with ProcessPoolExecutor(max_workers=4) as pool:
        for label, n, scenario, path, r in pool.map(run, specs):
            results[label, n, scenario] = r
            receipts.append({'variant': label, 'substeps': n, 'scenario': scenario,
                'path': path, 'sha256': digest(Path(path)), 'metrics': r['metrics'],
                'tail': tail_diagnostic(r['rows']) if r['rows'] else None})
            print(label, n, scenario, r['metrics']['failed'], flush=True)
    differences = []
    pairs = []
    for label in variants:
        for scenario in ('nominal', 'sustained_release_normal'):
            coarse = results[label, 4, scenario]
            fine = results[label, 8, scenario]
            if coarse['metrics']['failed'] or fine['metrics']['failed']:
                differences.append({'variant': label, 'scenario': scenario, 'failed': True})
            else:
                differences.append({'variant': label, 'scenario': scenario,
                    'difference': refinement_error(coarse['rows'], fine['rows'],
                        coarse_dt_s=.002, fine_dt_s=.002)})
        for n in (4, 8):
            pairs.append({'variant': label, 'substeps': n,
                'pair': compare_pair(results[label, n, 'nominal'],
                                     results[label, n, 'sustained_release_normal'])})
    write(args.output/'summary.json', {'receipts': receipts, 'refinement': differences,
        'pairs': pairs, 'scope': 'development mechanism ablation; no independent final validation',
        'formal_campaign_complete': False, 'live_executed': False})
    write(args.output/'manifest.json', {'sha256': {p.name: digest(p)
        for p in args.output.iterdir() if p.is_file()}})


if __name__ == '__main__':
    main()
