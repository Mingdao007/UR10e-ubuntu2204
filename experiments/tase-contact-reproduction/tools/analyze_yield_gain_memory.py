"""Recompute GM-v1 tables and memory excitation from immutable full-state receipts."""
import argparse
import json
from pathlib import Path
import shutil
import numpy as np
from contact_laws import ContactLawSnapshot
from run_contact_yield import read, write


def activation(path):
    r = read(path)
    windows = {'nominal_pre': [], 'sustained_hold': [], 'post_release_tail': []}
    for rec in r['records']:
        ref = rec['reference']
        if ref['phase'] != 'path':
            continue
        t = ref['path_time_s']
        name = 'nominal_pre' if 5 <= t < 15 else 'sustained_hold' if 20.5 <= t < 30.5 else 'post_release_tail' if t >= 52.84 else None
        if name is None:
            continue
        s = rec['controller_snapshot']['law22']
        token = ContactLawSnapshot(s['values'], binding_id=s['binding_id'])
        out = rec['result']
        windows[name].append((min(token.metric_eigenvalues),
            np.linalg.norm(token.force_history), np.linalg.norm(out['law_force_base_n']),
            np.linalg.norm(out['force_residual_base_n'])))
    return {'path': str(Path(path).resolve()), 'scenario': r['scenario'],
        'windows': {name: {'samples': len(v), 'metric_eigenvalue_min': float(np.min(np.array(v)[:, 0])),
            'metric_eigenvalue_mean': float(np.mean(np.array(v)[:, 0])),
            'history_norm_mean_n': float(np.mean(np.array(v)[:, 1])),
            'law_input_norm_mean_n': float(np.mean(np.array(v)[:, 2])),
            'pre_restoring_residual_norm_mean_n': float(np.mean(np.array(v)[:, 3]))}
            for name, v in windows.items() if v}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runs', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args();a.output.mkdir(parents=True, exist_ok=True)
    x = read(a.runs/'summary.json')
    for name in ('summary.json', 'protocol.json', 'manifest.json'):
        shutil.copy2(a.runs/name, a.output/name)
    lines = ['# GM-v1 measured results', '',
        'Development-only factorial ablation. Beta=1 removes mechanical memory coupling; it does not reset state.', '',
        '| Variant | Scenario | Plant substeps | Force MAE N | Peak N | Path RMS mm | Progress | Tail force std N | Tail dominant Hz |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for c in x['receipts']:
        m = c['metrics'];t = c['tail']
        lines.append(f"| {c['variant']} | {c['scenario']} | {c['substeps']} | {m['force_mae_n']:.6f} | {m['force_peak_n']:.6f} | {1000*m['path_rmse_m']:.4f} | {m['progress_ratio']:.6f} | {t['load_std_n']:.6f} | {t['dominant_frequency_hz']:.3f} |")
    lines += ['', '## Unaligned refinement', '',
        '| Variant | Scenario | Max force difference N | Max TCP difference mm |', '|---|---|---:|---:|']
    for c in x['refinement']:
        d = c['difference']
        lines.append(f"| {c['variant']} | {c['scenario']} | {d['force_error_max_n']:.6f} | {1000*d['position_max_m']:.6f} |")
    lines += ['', '## Matched nominal/release (8 substeps)', '',
        '| Variant | Recovery s | Recoil mm | Residual mm |', '|---|---:|---:|---:|']
    for c in x['pairs']:
        if c['substeps'] != 8:
            continue
        v = c['pair'];recovery = 'censored' if v['right_censored'] else f"{v['recovery_s']:.4f}"
        lines.append(f"| {c['variant']} | {recovery} | {1000*v['recoil_m']:.4f} | {1000*v['residual_displacement_m']:.4f} |")
    (a.output/'results.md').write_text('\n'.join(lines)+'\n')
    cases = [c for c in x['receipts'] if c['substeps'] == 8 and c['variant'].endswith('-on')]
    values = []
    for c in cases:
        values.append({'variant': c['variant'], **activation(c['path'])})
    old = a.runs.parent/'yield-offset-ablation/MSFC-sustained_release_tangent.json.gz'
    values.append({'variant': 'retained-g100-on-tangent', **activation(old)})
    write(a.output/'memory-activation.json', {'cases': values,
        'claim_scope': 'full-state excitation audit; development data, not final validation'})
    print(json.dumps(values, indent=2))


if __name__ == '__main__':
    main()
