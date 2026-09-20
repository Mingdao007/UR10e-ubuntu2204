"""FP-v1 event traces and same-method nominal deviations."""
import argparse
import gc
import gzip
import hashlib
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def load(row):
    path = Path(row['path'])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']
    with gzip.open(path, 'rt') as f:
        return json.load(f)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', type=Path, required=True)
    a = p.parse_args()
    results = json.loads((a.report/'results.json').read_text())['runs']
    protocol = json.loads((a.report/'protocol.json').read_text())
    fig, axes = plt.subplots(2, 2, figsize=(11, 7), constrained_layout=True)
    colors = ('#777777', '#277da8', '#b46431', '#6c4089')
    for row, color in zip(results, colors):
        run = load(row); nominal = load(protocol['nominal'][row['variant']])
        t = np.asarray([x['time_s'] for x in run['rows']])
        select = (t >= 19.5) & (t <= 27)
        force = np.asarray([x['true_normal_load_n'] for x in run['rows']])
        position = np.asarray([x['position_m'] for x in run['rows']])
        nominal_position = np.asarray([x['position_m'] for x in nominal['rows']])
        distance = np.linalg.norm(position - nominal_position, axis=1)*1000
        axes[0, 0].plot(t[select], force[select], color=color, label=row['variant'])
        axes[0, 1].plot(t[select], distance[select], color=color)
        axes[1, 0].scatter(row['event']['force_peak_n'], row['pair']['recovery_s'],
                           color=color, s=55, label=row['variant'])
        axes[1, 0].annotate(row['variant'], (row['event']['force_peak_n'], row['pair']['recovery_s']),
                            xytext=(5, 5), textcoords='offset points', fontsize=8)
        if row['method']=='MSFC':
            formal = [r for r in run['records'] if r['reference']['phase']=='path']
            assert np.allclose([r['reference']['path_time_s'] for r in formal], t,
                               rtol=0, atol=1e-12)
            eigen = np.asarray([r['controller_snapshot']['law22']['values'][19:22]
                                for r in formal])
            axes[1, 1].plot(t[select], eigen.min(axis=1)[select], color=color, label=row['variant'])
        del run, nominal; gc.collect()
    for ax in (axes[0, 0], axes[0, 1], axes[1, 1]):
        ax.axvspan(20, 20.5, color='black', alpha=.08)
        ax.set_xlabel('Time (s)'); ax.grid(alpha=.2)
    axes[0, 0].axhline(5, color='black', lw=.7, ls='--')
    axes[0, 0].set_ylabel('True simulated normal load (N)')
    axes[0, 0].legend(fontsize=8)
    axes[0, 1].axhline(2, color='black', lw=.7, ls='--')
    axes[0, 1].set_ylabel('TCP distance from matched nominal (mm)')
    axes[1, 0].set_xlabel('Event contact peak (N)')
    axes[1, 0].set_ylabel('Paired recovery (s)'); axes[1, 0].grid(alpha=.2)
    axes[1, 0].margins(.3)
    axes[1, 1].set_ylabel('Minimum metric eigenvalue'); axes[1, 1].legend(fontsize=8)
    fig.suptitle('FP-v1: fixed 3 N, 0.5 s oblique pulse with frozen normal observer\n'
                 'Development simulation; untuned candidates. Identity metric retains force-history states.', fontsize=12)
    for ext in ('png', 'pdf', 'svg'):
        fig.savefig(a.report/f'comparison.{ext}', dpi=160)
    svg = a.report/'comparison.svg'
    svg.write_text('\n'.join(x.rstrip() for x in svg.read_text().splitlines())+'\n')


if __name__=='__main__':
    main()
