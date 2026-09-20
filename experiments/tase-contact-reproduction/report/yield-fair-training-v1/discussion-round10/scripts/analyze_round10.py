"""Round-10 advisory analysis over a hash-verified subset of the terminal training campaign.

Reads /tmp/yfp10/<id>.npz + .meta.json produced by extract_rows_v10.py (each verified against
report/yield-training-report-final-v1/artifact_digest_manifest.csv), the committed campaign config
(read-only), the committed final report CSVs (read-only) and the frozen evaluator functions
(read-only import from tools/).  Writes JSON to stdout.  No run, no ledger access, no artifact
modification.  Subset: shared initial triples 0..4 for SFC/DSFC/MSFC and the shared EI triple
DSFC-08 / MSFC-08 (17 pairs, 34 members).
"""
import csv, json, math, sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'tools'))
from yield_fair_selection import load_contract, cell_weight, _objective_components  # noqa: E402
from contact_yield_metrics import summarize_trial, compare_pair  # noqa: E402

CACHE = Path('/tmp/yfp10')
FINAL = ROOT / 'report/yield-training-report-final-v1'
C = load_contract(ROOT / 'config/yield_fair_campaign_v1.json')
ONSET, RAMP_END, HOLD_END, RELEASE = 20.0, 20.5, 30.5, 35.5
T = C.period_s
PHASES = (('pre', 0.0, ONSET), ('onset', ONSET, RAMP_END), ('hold', RAMP_END, HOLD_END), ('release', HOLD_END, RELEASE), ('post', RELEASE, T))
DEG = 180.0 / math.pi
PAIRS = [(m, u) for u in range(5) for m in ('SFC', 'DSFC', 'MSFC')] + [('DSFC', 8), ('MSFC', 8)]

MANIFEST = {r['attempt_id']: r for r in csv.DictReader((FINAL / 'artifact_digest_manifest.csv').open())}
SLOTS = {(r['method'], int(r['unit'])): r for r in csv.DictReader((FINAL / 'slots.csv').open())}
MEMBERS = {(r['method'], int(r['unit']), r['condition']): r for r in csv.DictReader((FINAL / 'members.csv').open())}


def f(x):
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else x


def load(aid):
    z = np.load(CACHE / f'{aid}.npz')
    d = {k: z[k] for k in z.files}
    meta = json.loads((CACHE / f'{aid}.meta.json').read_text())
    if meta['_extract']['artifact_sha256'] != MANIFEST[aid]['artifact_sha256']:
        raise SystemExit(f'{aid}: cache sha does not match final manifest')
    return d, meta


def rows_for_metrics(d):
    n = len(d['row_time_s'])
    keys = ('time_s', 'true_normal_load_n', 'force_error_n', 'path_error_m', 'orientation_error_rad',
            'actual_progress_m_s', 'reference_progress_m_s', 'normal_estimation_error_rad')
    cols = {k: d['row_' + k].tolist() for k in keys}
    sat = d['row_saturated'].tolist(); qp = d['row_qp_intervention'].tolist(); pos = d['row_position_m'].tolist()
    return [{**{k: cols[k][i] for k in keys}, 'dt_s': C.dt_s, 'saturated': sat[i], 'qp_intervention': qp[i], 'position_m': pos[i]} for i in range(n)]


def art_for_pair(d, meta, rows):
    return {'method': meta['method'], 'material': meta['material'], 'dt_s': meta['dt_s'], 'duration_s': meta['duration_s'],
            'preparation': meta['preparation'], 'timeline': meta['timeline'], 'identity': meta['identity'],
            'scenario': meta['scenario'], 'metrics': meta['metrics'], 'rows': rows}


def mask(t, a, b, name):
    return (t >= a) & (t < b) if name != 'post' else (t >= a)


def rms(x):
    return f(np.sqrt(np.mean(x * x))) if len(x) else None


def law_angle_deg(d, m):
    v = d['res_law_velocity_base_m_s'][m]; F = d['res_law_force_base_n'][m]
    nv = np.linalg.norm(v, axis=1); nF = np.linalg.norm(F, axis=1)
    ok = (nv > 1e-9) & (nF > 1e-9)
    if not ok.any():
        return None
    cosang = np.clip(np.einsum('ij,ij->i', v[ok], F[ok]) / (nv[ok] * nF[ok]), -1, 1)
    return f(np.median(np.degrees(np.arccos(cosang))))


def member_phase_descriptors(d):
    t = d['row_time_s']; dt = C.dt_s
    load = d['row_true_normal_load_n']; err = d['row_force_error_n']; path = d['row_path_error_m']
    att = d['row_orientation_error_rad']; nerr = d['row_normal_estimation_error_rad']
    v = d['row_actual_progress_m_s']; vr = d['row_reference_progress_m_s']
    out = {}
    for name, a, b in PHASES:
        m = mask(t, a, b, name)
        req = float(np.sum(vr[m]) * dt)
        out[name] = {
            'load_mean_n': f(load[m].mean()), 'load_min_n': f(load[m].min()), 'load_max_n': f(load[m].max()),
            'load_min_time_s': f(t[m][int(np.argmin(load[m]))]), 'load_max_time_s': f(t[m][int(np.argmax(load[m]))]),
            'force_mae_n': f(np.mean(np.abs(err[m]))), 'force_error_peak_n': f(np.max(np.abs(err[m]))),
            'path_rms_mm': f(1e3 * rms(path[m])), 'path_peak_mm': f(1e3 * path[m].max()),
            'attitude_rms_deg': f(DEG * rms(att[m])), 'attitude_peak_deg': f(DEG * att[m].max()),
            'normal_estimation_rms_deg': f(DEG * rms(nerr[m])),
            'progress_ratio': f(float(np.sum(v[m]) * dt) / req) if req > 1e-12 else None,
            'saturation_s': f(d['row_saturated'][m].sum() * dt),
            'normal_saturated_s': f(d['res_normal_saturated'][m].sum() * dt),
            'tangent_saturation_s': f((d['res_tangent_saturation_m_s'][m] > 0).sum() * dt),
            'qp_intervention_s': f(d['row_qp_intervention'][m].sum() * dt),
            'task_scale_mean': f(d['row_task_scale'][m].mean()), 'task_scale_min': f(d['row_task_scale'][m].min()),
            'motion_updates': int(d['est_motion_update_applied'][m].sum()),
            'law_velocity_force_median_angle_deg': law_angle_deg(d, m),
            'low_load_below_1n_s': f((load[m] < 1.0).sum() * dt),
        }
    i_peak = int(np.argmax(load))
    out['whole'] = {'load_peak_n': f(load[i_peak]), 'load_peak_time_s': f(t[i_peak]),
                    'load_max_after_onset_n': f(load[t >= ONSET].max()), 'load_min_n': f(load.min()), 'load_min_time_s': f(t[int(np.argmin(load))]),
                    'attitude_rms_deg': f(DEG * rms(att)), 'normal_estimation_rms_deg': f(DEG * rms(nerr)),
                    'path_rms_mm': f(1e3 * rms(path)), 'saturation_s': f(d['row_saturated'].sum() * dt),
                    'normal_saturated_s': f(d['res_normal_saturated'].sum() * dt), 'qp_intervention_s': f(d['row_qp_intervention'].sum() * dt)}
    # early nominal segments (entry transient lives in the first second of the PATH clock after the 1 s entry)
    for name, a, b in (('first_1s', 0.0, 1.0), ('1_to_3s', 1.0, 3.0), ('3_to_10s', 3.0, 10.0), ('10_to_20s', 10.0, 20.0)):
        m = (t >= a) & (t < b)
        out[name] = {'load_mean_n': f(load[m].mean()), 'load_min_n': f(load[m].min()), 'load_max_n': f(load[m].max()),
                     'force_mae_n': f(np.mean(np.abs(err[m]))), 'path_rms_mm': f(1e3 * rms(path[m])),
                     'attitude_rms_deg': f(DEG * rms(att[m])), 'normal_estimation_rms_deg': f(DEG * rms(nerr[m])),
                     'law_velocity_force_median_angle_deg': law_angle_deg(d, m)}
    return out


def metric_engagement(d, meta):
    """MSFC metric eigenvalues: law22 slots 19..21 (PATH records) and entry records."""
    if meta['method'] != 'MSFC':
        return None
    t = d['row_time_s']; lam = d['rec_law22'][:, 19:22]; lam_e = d['entry_law22'][:, 19:22]
    assert np.all(lam > 0) and np.all(lam <= 1.0 + 1e-12)
    lmin = lam.min(axis=1); lmin_e = lam_e.min(axis=1)
    out = {'entry_lambda_min_minimum': f(lmin_e.min()), 'entry_lambda_min_final': f(lmin_e[-1]),
           'path_t0_lambda_min': f(lmin[0]), 'whole_lambda_min_minimum': f(lmin.min()), 'whole_lambda_min_minimum_time_s': f(t[int(np.argmin(lmin))]),
           'seconds_below_0p95_total': f((lmin < 0.95).sum() * C.dt_s), 'seconds_below_0p99_total': f((lmin < 0.99).sum() * C.dt_s),
           'lambda_min_at_onset_tick': f(lmin[int(np.searchsorted(t, ONSET)) - 1]), 'lambda_min_at_release': f(lmin[int(np.searchsorted(t, RELEASE))]),
           'lambda_min_final': f(lmin[-1]), 'h_norm_final': f(np.linalg.norm(d['rec_law22'][-1, 7:10])), 'S_fro_final': f(np.linalg.norm(d['rec_law22'][-1, 10:19]))}
    for name, a, b in PHASES:
        m = mask(t, a, b, name)
        out[name] = {'lambda_min_minimum': f(lmin[m].min()), 'lambda_min_mean': f(lmin[m].mean()), 'seconds_below_0p95': f((lmin[m] < 0.95).sum() * C.dt_s),
                     'seconds_below_0p99': f((lmin[m] < 0.99).sum() * C.dt_s)}
    edges = [0, 1, 2, 3, 5, 10, 15, 20, 20.5, 22, 25, 30.5, 32, 35.5, 40, 50, T]
    out['binned_lambda_min_mean'] = [{'from_s': edges[i], 'to_s': edges[i + 1], 'lambda_min_mean': f(lmin[(t >= edges[i]) & (t < edges[i + 1])].mean()),
                                      'lambda_min_minimum': f(lmin[(t >= edges[i]) & (t < edges[i + 1])].min())} for i in range(len(edges) - 1)]
    return out


def phase_decomposition(dn, dd):
    t = dn['row_time_s']; dt = C.dt_s
    load = np.abs(dd['row_true_normal_load_n'] - dn['row_true_normal_load_n']) / C.jload_scale_n
    path = np.linalg.norm(dd['row_position_m'] - dn['row_position_m'], axis=1) / C.jpath_scale_m
    att = np.abs(dd['row_orientation_error_rad'] - dn['row_orientation_error_rad']) / C.jatt_scale_rad
    w_load = np.asarray([cell_weight(float(x), dt, C.load_onset_s, T, T) for x in t])
    w_path = np.asarray([cell_weight(float(x), dt, C.path_release_s, T, T) for x in t])
    w_att = np.asarray([cell_weight(float(x), dt, C.attitude_onset_s, T, T) for x in t])
    # absolute (unpaired) counterparts on the same windows/scales: what the disturbed and nominal members cost on their own
    att_d = np.abs(dd['row_orientation_error_rad']) / C.jatt_scale_rad; att_n = np.abs(dn['row_orientation_error_rad']) / C.jatt_scale_rad
    path_d = dd['row_path_error_m'] / C.jpath_scale_m; path_n = dn['row_path_error_m'] / C.jpath_scale_m
    out = {}
    for name, a, b in PHASES:
        m = mask(t, a, b, name)
        out[name] = {'Jload': f((load * w_load)[m].sum()), 'Jpath': f((path * w_path)[m].sum()), 'Jatt': f((att * w_att)[m].sum()),
                     'paired_path_band_s_unweighted': f((path * dt)[m].sum()),
                     'mean_abs_load_diff_n': f((load * C.jload_scale_n)[m].mean()), 'mean_pos_diff_mm': f((path * C.jpath_scale_m)[m].mean() * 1e3),
                     'mean_abs_att_diff_deg': f((att * C.jatt_scale_rad)[m].mean() * DEG),
                     'A_att_disturbed_band_s': f((att_d * dt)[m].sum()), 'A_att_nominal_band_s': f((att_n * dt)[m].sum()),
                     'A_path_disturbed_band_s': f((path_d * dt)[m].sum()), 'A_path_nominal_band_s': f((path_n * dt)[m].sum())}
    return out


def pair_summary(method, unit, dn, dd, metan, metad):
    rn, rd = rows_for_metrics(dn), rows_for_metrics(dd)
    comp = _objective_components(rn, rd, C)
    rec = compare_pair(art_for_pair(dn, metan, rn), art_for_pair(dd, metad, rd))
    slot = SLOTS[(method, unit)]
    t = dd['row_time_s']; post = t >= RELEASE; pre = t < ONSET
    dist = np.linalg.norm(dd['row_position_m'] - dn['row_position_m'], axis=1)
    i_rel = int(np.searchsorted(t, RELEASE)); i_pmax = int(np.argmax(np.where(post, dist, -1)))
    ld, ln = dd['row_true_normal_load_n'], dn['row_true_normal_load_n']
    reg_rec = slot['recovery_s']
    return {
        'method': method, 'unit': unit, 'shared_initial_triple': slot['shared_initial_triple'] == 'True',
        'candidate': metan['identity_payload']['parameters'], 'slot_state': slot['slot_state'], 'pair_feasible': slot['pair_feasible'] == 'True',
        'J_recomputed': comp, 'J_registered': {k: float(slot[k]) for k in ('J', 'Jload', 'Jpath', 'Jatt')},
        'J_abs_diff': f(abs(comp['J'] - float(slot['J']))),
        'recovery_recomputed_s': f(rec.get('recovery_s')), 'recovery_registered_s': f(reg_rec) if reg_rec else None,
        'right_censored': rec.get('right_censored'), 'residual_displacement_registered_m': f(slot['residual_displacement_m']),
        'residual_displacement_recomputed_m': f(rec.get('residual_displacement_m')),
        'distance_at_release_mm': f(dist[i_rel] * 1e3), 'max_post_release_offset_mm': f(dist[i_pmax] * 1e3), 'max_post_release_offset_time_s': f(t[i_pmax]),
        'registered_postrelease_max_offset_mm': f(float(slot['postrelease_max_offset_m']) * 1e3),
        'pre_onset_rows_bitwise_equal': bool(np.array_equal(dd['row_position_m'][pre], dn['row_position_m'][pre]) and np.array_equal(ld[pre], ln[pre])
                                             and np.array_equal(dd['row_orientation_error_rad'][pre], dn['row_orientation_error_rad'][pre])),
        'phases': phase_decomposition(dn, dd),
        'load_min_disturbed_n': f(ld.min()), 'load_min_disturbed_time_s': f(t[int(np.argmin(ld))]),
        'load_max_after_onset_disturbed_n': f(ld[t >= ONSET].max()), 'load_max_after_onset_nominal_n': f(ln[t >= ONSET].max()),
        'whole_episode_peak_equal_nominal': bool(ld.max() == ln.max()),
        'hold_mean_load_disturbed_n': f(ld[(t >= RAMP_END) & (t < HOLD_END)].mean()), 'hold_mean_load_nominal_n': f(ln[(t >= RAMP_END) & (t < HOLD_END)].mean()),
        'terminal_offset_mm': f(dist[-1] * 1e3),
    }


def main():
    members, pairs, checks = {}, {}, []
    data = {}
    for method, unit in PAIRS:
        for cond in ('nominal', 'disturbed'):
            aid = f'{method}-{unit:02d}-{cond}'
            d, meta = load(aid)
            data[aid] = (d, meta)
            summ = summarize_trial(rows_for_metrics(d), scenario=meta['scenario'], material=meta['material'], method=meta['method'], dt_s=meta['dt_s'],
                                   kinematics_kind=meta['kinematics_kind'], campaign_kind=meta['campaign_kind'], timeline=meta['timeline'])
            summ['load_min_n'] = float(d['row_true_normal_load_n'].min())
            diffs = {k: (summ[k], meta['metrics'].get(k)) for k in ('force_mae_n', 'force_peak_n', 'path_rmse_m', 'orientation_rmse_rad', 'progress_ratio', 'saturation_ticks', 'qp_intervention_ticks')
                     if summ[k] != meta['metrics'].get(k)}
            csvrow = MEMBERS[(method, unit, cond)]
            csv_diffs = {k: (summ[k], float(csvrow[c])) for k, c in (('force_mae_n', 'force_mae_n'), ('force_peak_n', 'force_peak_n'), ('load_min_n', 'load_min_n'), ('path_rmse_m', 'path_rms_m'), ('orientation_rmse_rad', 'attitude_rms_rad'), ('progress_ratio', 'progress_ratio'))
                         if abs(summ[k] - float(csvrow[c])) > 1e-12}
            checks.append({'id': aid, 'sha256': meta['_extract']['artifact_sha256'], 'summarize_trial_equals_artifact_metrics': not diffs,
                           'summarize_trial_equals_members_csv_1e-12': not csv_diffs, 'diffs': {k: [f(a), f(b)] for k, (a, b) in diffs.items()}, 'csv_diffs': {k: [f(a), f(b)] for k, (a, b) in csv_diffs.items()}})
            members[aid] = {'method': method, 'unit': unit, 'condition': cond, 'sha256': meta['_extract']['artifact_sha256'],
                            'candidate': meta['identity_payload']['parameters'], 'phases': member_phase_descriptors(d), 'metric_engagement': metric_engagement(d, meta)}
    for method, unit in PAIRS:
        dn, metan = data[f'{method}-{unit:02d}-nominal']; dd, metad = data[f'{method}-{unit:02d}-disturbed']
        pairs[f'{method}-{unit:02d}'] = pair_summary(method, unit, dn, dd, metan, metad)

    # cross-arm deltas at shared triples
    def delta(a, b, keys):
        return {k: (None if a.get(k) is None or b.get(k) is None else f(a[k] - b[k])) for k in keys}
    contrasts = {}
    for unit in (0, 1, 2, 3, 4, 8):
        row = {}
        for (x, y) in (('MSFC', 'DSFC'), ('SFC', 'DSFC'), ('SFC', 'MSFC')):
            if (x, unit) not in SLOTS or (y, unit) not in SLOTS or unit == 8 and x == 'SFC' or unit == 8 and y == 'SFC':
                continue
            px, py = pairs[f'{x}-{unit:02d}'], pairs[f'{y}-{unit:02d}']
            entry = {'J': delta(px['J_recomputed'], py['J_recomputed'], ('J', 'Jload', 'Jpath', 'Jatt')),
                     'recovery_s': None if px['recovery_recomputed_s'] is None or py['recovery_recomputed_s'] is None else f(px['recovery_recomputed_s'] - py['recovery_recomputed_s']),
                     'max_post_release_offset_mm': f(px['max_post_release_offset_mm'] - py['max_post_release_offset_mm']),
                     'load_min_disturbed_n': f(px['load_min_disturbed_n'] - py['load_min_disturbed_n']),
                     'phases': {}}
            for name, _, _ in PHASES:
                entry['phases'][name] = {
                    'paired': delta(px['phases'][name], py['phases'][name], ('Jload', 'Jpath', 'Jatt', 'mean_abs_att_diff_deg', 'mean_pos_diff_mm', 'mean_abs_load_diff_n')),
                    'disturbed_absolute': delta(members[f'{x}-{unit:02d}-disturbed']['phases'][name], members[f'{y}-{unit:02d}-disturbed']['phases'][name],
                                                ('load_min_n', 'load_mean_n', 'force_mae_n', 'force_error_peak_n', 'path_rms_mm', 'attitude_rms_deg', 'attitude_peak_deg', 'normal_estimation_rms_deg', 'progress_ratio', 'saturation_s', 'normal_saturated_s', 'tangent_saturation_s', 'qp_intervention_s', 'task_scale_mean')),
                    'nominal_absolute': delta(members[f'{x}-{unit:02d}-nominal']['phases'][name], members[f'{y}-{unit:02d}-nominal']['phases'][name],
                                              ('load_min_n', 'load_max_n', 'load_mean_n', 'force_mae_n', 'force_error_peak_n', 'path_rms_mm', 'attitude_rms_deg', 'normal_estimation_rms_deg', 'progress_ratio', 'saturation_s', 'qp_intervention_s'))}
            row[f'{x}_minus_{y}'] = entry
        contrasts[f'unit_{unit}'] = row

    out = {'scope': 'round-10 advisory post-processing of 34 hash-verified members of the terminal training campaign (shared triples 0..4 and the shared EI triple DSFC-08/MSFC-08); development data; no run; training unchanged',
           'contract': {'bands': {'path_rms_m': C.nominal_path_rms_m_max, 'progress_ratio': C.nominal_progress_ratio_min, 'attitude_rms_rad': C.nominal_attitude_rms_rad_max, 'force_mae_n': C.nominal_load_mae_n_max, 'force_peak_n': C.nominal_load_peak_n_max, 'load_min_n': C.nominal_load_min_n_min},
                        'guards': {'load_min_n': C.disturbed_load_min_n_min, 'force_peak_n': C.disturbed_load_peak_n_max, 'progress_ratio': C.disturbed_progress_ratio_min},
                        'scales': {'Jload_n': C.jload_scale_n, 'Jpath_m': C.jpath_scale_m, 'Jatt_rad': C.jatt_scale_rad},
                        'windows': {'load_onset_s': C.load_onset_s, 'path_release_s': C.path_release_s, 'attitude_onset_s': C.attitude_onset_s, 'period_s': T},
                        'phases_s': {n: [a, b] for n, a, b in PHASES}, 'recovery_bands': {'path_m': 0.002, 'force_n': 0.5}},
           'member_checks': checks,
           'all_summaries_equal_artifact_metrics': all(c['summarize_trial_equals_artifact_metrics'] for c in checks),
           'all_summaries_equal_members_csv': all(c['summarize_trial_equals_members_csv_1e-12'] for c in checks),
           'all_J_equal_registered': all(p['J_abs_diff'] == 0.0 for p in pairs.values()),
           'max_J_abs_diff': max(p['J_abs_diff'] for p in pairs.values()),
           'all_recovery_equal_registered': all((p['recovery_recomputed_s'] is None and p['recovery_registered_s'] is None) or (p['recovery_recomputed_s'] is not None and p['recovery_registered_s'] is not None and abs(p['recovery_recomputed_s'] - p['recovery_registered_s']) < 1e-9) for p in pairs.values()),
           'all_pre_onset_rows_bitwise_equal': all(p['pre_onset_rows_bitwise_equal'] for p in pairs.values()),
           'all_whole_episode_peaks_equal_nominal': all(p['whole_episode_peak_equal_nominal'] for p in pairs.values()),
           'members': members, 'pairs': pairs, 'contrasts': contrasts}
    json.dump(out, sys.stdout, indent=1, sort_keys=False)


if __name__ == '__main__':
    main()
