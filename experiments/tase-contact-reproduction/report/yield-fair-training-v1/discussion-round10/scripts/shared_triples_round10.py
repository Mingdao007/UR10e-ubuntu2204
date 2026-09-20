"""All exactly-shared registered (m, mu, g) triples in the terminal campaign, from the final report CSVs only.

Exact string equality of the registered floats in slots.csv; no raw artifact is read.  Writes JSON to stdout.
"""
import csv, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
FINAL = ROOT / 'report/yield-training-report-final-v1'
slots = [r for r in csv.DictReader((FINAL / 'slots.csv').open()) if r['m'] and r['literal_repeat'] != 'True']
members = {(r['method'], int(r['unit']), r['condition']): r for r in csv.DictReader((FINAL / 'members.csv').open())}


def fl(x):
    return None if x in ('', None) else float(x)


by = {}
for r in slots:
    by.setdefault((r['m'], r['mu'], r['g']), []).append(r)
NOM = ('force_mae_n', 'force_peak_n', 'load_min_n', 'path_rms_m', 'attitude_rms_rad', 'progress_ratio', 'saturation_ticks', 'qp_intervention_ticks')
DIS = ('force_error_peak_n', 'load_min_n', 'path_rms_m', 'attitude_rms_rad', 'attitude_peak_rad', 'progress_ratio', 'low_load_duration_s', 'saturation_ticks', 'qp_intervention_ticks')
out = []
for key, rs in by.items():
    meths = {r['method']: r for r in rs}
    if len(meths) < 2:
        continue
    rec = {'m': float(key[0]), 'mu': float(key[1]), 'g': float(key[2]), 'slots': {m: {'unit': int(r['unit']), 'phase': r['scheduled_phase'], 'state': r['slot_state'], 'pair_feasible': r['pair_feasible'] == 'True',
                                                                                 'J': fl(r['J']), 'Jload': fl(r['Jload']), 'Jpath': fl(r['Jpath']), 'Jatt': fl(r['Jatt']), 'recovery_s': fl(r['recovery_s']),
                                                                                 'postrelease_max_offset_mm': None if fl(r['postrelease_max_offset_m']) is None else 1e3 * fl(r['postrelease_max_offset_m'])} for m, r in meths.items()}}
    for m, r in meths.items():
        u = int(r['unit'])
        rec['slots'][m]['nominal'] = {k: fl(members[(m, u, 'nominal')][k]) for k in NOM}
        rec['slots'][m]['disturbed'] = {k: fl(members[(m, u, 'disturbed')][k]) for k in DIS}
    for a, b in (('MSFC', 'DSFC'), ('SFC', 'DSFC')):
        if a in meths and b in meths:
            sa, sb = rec['slots'][a], rec['slots'][b]
            rec[f'{a}_minus_{b}'] = {k: (None if sa[k] is None or sb[k] is None else sa[k] - sb[k]) for k in ('J', 'Jload', 'Jpath', 'Jatt', 'recovery_s', 'postrelease_max_offset_mm')}
            rec[f'{a}_minus_{b}']['nominal'] = {k: sa['nominal'][k] - sb['nominal'][k] for k in NOM}
            rec[f'{a}_minus_{b}']['disturbed'] = {k: sa['disturbed'][k] - sb['disturbed'][k] for k in DIS}
    out.append(rec)
out.sort(key=lambda r: min(s['unit'] for s in r['slots'].values()))
summary = {'shared_triples_total': len(out), 'DSFC_MSFC_shared': sum('MSFC_minus_DSFC' in r for r in out), 'three_way_shared': sum(len(r['slots']) == 3 for r in out),
           'ei_phase_coincidences': [(r['slots']['DSFC']['unit'], r['slots']['MSFC']['unit']) for r in out if 'MSFC' in r['slots'] and 'DSFC' in r['slots'] and r['slots']['DSFC']['phase'] == 'bayesian_ei'],
           'mechanism_note': 'the frozen EI proposer scores a fixed 512-point scrambled-Sobol acquisition pool (seed 20260920+1009) shared by all methods, so exact coincidences are structural, not accidental'}
json.dump({'summary': summary, 'triples': out}, sys.stdout, indent=1)
