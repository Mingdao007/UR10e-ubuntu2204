"""Recompute compact tables and figures from retained mechanism receipts."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--refinement', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--label', default='Unqualified offline mechanism screen')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    summary = json.loads((args.runs / 'summary.json').read_text())
    refinement = json.loads((args.refinement / 'summary.json').read_text())
    for name in ('summary.json', 'screen.json', 'manifest.json'):
        shutil.copy2(args.runs / name, args.output / name)
    (args.output / 'plant-refinement.json').write_text(
        json.dumps(refinement, indent=2, allow_nan=False) + '\n')
    lines = ['# Measured offline mechanism results', '', args.label, '',
             'Simulation only. No physical result, formal holdout or declared winner.', '',
             'Force peak is actual model contact load, not force-error peak. '
             'Progress is measured signed TCP motion projected onto the reference tangent; '
             'it is not an independent completion detector.', '',
             '| Method | Material | Scenario | Force MAE N | Peak N | Path RMS mm | Progress ratio | Saturation % | QP intervention % | Full cycle |',
             '|---|---|---|---:|---:|---:|---:|---:|---:|---|']
    for row in summary['runs']:
        m = row['metrics']
        n = max(1, m['n_samples'])
        values = [row['method'], row['material'], row['scenario'],
                  f"{m['force_mae_n']:.4f}", f"{m['force_peak_n']:.4f}",
                  f"{1000*m['path_rmse_m']:.3f}", f"{m['progress_ratio']:.4f}",
                  f"{100*m['saturation_ticks']/n:.2f}",
                  f"{100*m['qp_intervention_ticks']/n:.2f}", str(row['full_cycle'])]
        lines.append('| ' + ' | '.join(values) + ' |')
    lines += ['', '## Matched nominal versus intervention', '',
              'Recovery must occur after release and remain within 2 mm and 0.5 N of '
              'the same-method nominal through the remaining observation window '
              '(at least 0.1 s). Censored is not zero recovery time.', '',
              '| Method | Material | Scenario | Yield peak mm | Recoil mm | Residual mm | Recovery s |',
              '|---|---|---|---:|---:|---:|---:|']
    for row in summary['runs']:
        if 'pair' not in row:
            continue
        p = row['pair']
        if not p.get('eligible'):
            lines.append(f"| {row['method']} | {row['material']} | {row['scenario']} | ineligible | - | - | - |")
            continue
        recovery = 'censored' if p['right_censored'] else f"{p['recovery_s']:.3f}"
        lines.append(f"| {row['method']} | {row['material']} | {row['scenario']} | "
                     f"{1000*p['yield_peak_m']:.3f} | {1000*p['recoil_m']:.3f} | "
                     f"{1000*p['residual_displacement_m']:.3f} | {recovery} |")
    lines += ['', '## Plant integration sensitivity at fixed 2 ms control period', '',
              '| Method | Substeps | Max force difference from previous N | Max position difference mm |',
              '|---|---:|---:|---:|']
    for c in refinement['checks']:
        if 'force_difference_max_n' in c:
            lines.append(f"| {c['method']} | {c['plant_substeps']} | "
                         f"{c['force_difference_max_n']:.6f} | {1000*c['position_difference_max_m']:.6f} |")
    lines += ['', 'This sensitivity study has a short 0.6 s PATH horizon. '
              'It does not establish full-cycle ranking stability or hardware fidelity.', '',
              '## Artifact locations', '', f'Raw immutable receipts: `{args.runs.resolve()}`.', '',
              f'Integration receipts: `{args.refinement.resolve()}`.', '',
              'The copied manifest contains SHA-256 for each complete compressed full-state run. '
              'Controller/native binary/plant identities and source hashes are embedded in each receipt.']
    (args.output / 'results.md').write_text('\n'.join(lines) + '\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    methods = ('SFC', 'SFC_RADIAL', 'DSFC', 'MSFC')
    scenarios = ('nominal', 'sustained_release_normal', 'sustained_release_tangent', 'short_pulse_oblique')
    labels = ('Nominal', 'Sustained normal', 'Sustained tangent', 'Pulse oblique')
    colors = ('#3465a4', '#888a85', '#c17d11', '#75507b')
    for material in ('stiff_low_mu', 'compliant_high_mu'):
        fig, axes = plt.subplots(2, 2, figsize=(11, 7), constrained_layout=True)
        for ax, metric, scale, title in zip(axes.flat,
                ('force_mae_n', 'force_peak_n', 'path_rmse_m', 'progress_ratio'),
                (1, 1, 1000, 1),
                ('Contact force MAE (N)', 'Contact force peak (N)', 'Path RMS (mm)', 'Measured progress ratio')):
            for i, method in enumerate(methods):
                rows = [next(r for r in summary['runs'] if r['method'] == method
                             and r['material'] == material and r['scenario'] == s) for s in scenarios]
                ax.bar(np.arange(4) + (i-1.5)*.2,
                       [r['metrics'][metric]*scale for r in rows], .19,
                       label=method, color=colors[i])
            ax.set_xticks(np.arange(4), labels, rotation=12, fontsize=8)
            ax.set_title(title)
            ax.grid(axis='y', alpha=.2)
        axes[0, 0].legend(fontsize=8, ncol=2)
        fig.suptitle(f'{material}: {args.label}\nSimulation only; not qualified evidence', fontsize=12)
        fig.savefig(args.output / f'{material}.png', dpi=160)
        fig.savefig(args.output / f'{material}.pdf')
        plt.close(fig)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in args.output.iterdir() if p.is_file() and p.name != 'report-hashes.json'}
    (args.output / 'report-hashes.json').write_text(json.dumps(hashes, indent=2) + '\n')


if __name__ == '__main__':
    main()
