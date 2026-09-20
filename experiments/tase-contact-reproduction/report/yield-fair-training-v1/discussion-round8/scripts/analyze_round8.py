"""Round-8 advisory analysis over the fixed 25-attempt training snapshot.

Reads /tmp/yfp8/<id>.npz + .meta.json produced by extract_rows_v8.py (hash-verified
against report/yield-fair-training-v1/round8-input-snapshot.json), the frozen
campaign config (read-only) and the frozen evaluator functions (read-only import).
Writes JSON to stdout.  No run, no ledger access, no artifact modification.
"""
import json, math, sys, hashlib
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'tools'))
from yield_fair_selection import load_contract, cell_weight, _objective_components  # noqa: E402
from contact_yield_metrics import summarize_trial, compare_pair  # noqa: E402

SNAP = json.loads((ROOT / 'report/yield-fair-training-v1/round8-input-snapshot.json').read_text())
CACHE = Path('/tmp/yfp8')
C = load_contract(ROOT / 'config/yield_fair_campaign_v1.json')
METHODS = ('SFC', 'DSFC', 'MSFC')
ONSET, HOLD_END, RELEASE = 20.0, 30.5, 35.5
T = C.period_s
PHASES = (('pre', 0.0, ONSET), ('onset', ONSET, 20.5), ('hold', 20.5, HOLD_END), ('release', HOLD_END, RELEASE), ('post', RELEASE, T))
DEG = 180.0 / math.pi
BAND = {  # nominal bands (contract), for margins
    'path_rmse_m': ('<=', C.nominal_path_rms_m_max),
    'progress_ratio': ('>=', C.nominal_progress_ratio_min),
    'orientation_rmse_rad': ('<=', C.nominal_attitude_rms_rad_max),
    'force_mae_n': ('<=', C.nominal_load_mae_n_max),
    'force_peak_n': ('<=', C.nominal_load_peak_n_max),
    'load_min_n': ('>=', C.nominal_load_min_n_min),
}
GUARD = {
    'load_min_n': ('>=', C.disturbed_load_min_n_min),
    'force_peak_n': ('<=', C.disturbed_load_peak_n_max),
    'progress_ratio': ('>=', C.disturbed_progress_ratio_min),
}
CHECK_NAME = {'path_rmse_m': 'path_rms_ok', 'progress_ratio': 'progress_ok', 'orientation_rmse_rad': 'attitude_rms_ok',
              'force_mae_n': 'load_mae_ok', 'force_peak_n': 'load_peak_ok', 'load_min_n': 'load_min_ok'}


def f(x):
    return None if x is None else float(x)


def load(aid):
    z = np.load(CACHE / f'{aid}.npz')
    d = {k: z[k] for k in z.files}
    meta = json.loads((CACHE / f'{aid}.meta.json').read_text())
    return d, meta


def rows_for_metrics(d):
    """Minimal row dicts for the frozen summarize_trial / compare_pair / objective."""
    n = len(d['row_time_s'])
    keys = ('time_s', 'true_normal_load_n', 'force_error_n', 'path_error_m', 'orientation_error_rad',
            'actual_progress_m_s', 'reference_progress_m_s', 'normal_estimation_error_rad')
    cols = {k: d['row_' + k].tolist() for k in keys}
    sat = d['row_saturated'].tolist(); qp = d['row_qp_intervention'].tolist(); pos = d['row_position_m'].tolist()
    return [{**{k: cols[k][i] for k in keys}, 'dt_s': C.dt_s, 'saturated': sat[i], 'qp_intervention': qp[i],
             'position_m': pos[i]} for i in range(n)]


def member_summary(aid, d, meta, rows):
    rec = summarize_trial(rows, failed=False, failure_message=None, scenario=meta['scenario'], material=C.material,
                          method=meta['method'], dt_s=C.dt_s, kinematics_kind=meta['kinematics_kind'],
                          campaign_kind=C.campaign_kind, timeline=C.timeline)
    load = d['row_true_normal_load_n']; t = d['row_time_s']; att = d['row_orientation_error_rad']
    rec['load_min_n'] = float(load.min())
    rep = meta['metrics']
    diffs = {k: (rec[k], rep.get(k)) for k in ('path_rmse_m', 'progress_ratio', 'orientation_rmse_rad', 'force_mae_n', 'force_peak_n', 'force_rmse_n', 'path_peak_m', 'orientation_peak_rad', 'normal_estimation_rmse_rad', 'saturation_ticks', 'qp_intervention_ticks', 'actual_progress_m', 'reference_progress_m')
             if not (isinstance(rec[k], float) and isinstance(rep.get(k), float) and math.isclose(rec[k], rep[k], rel_tol=0, abs_tol=1e-12)) and rec[k] != rep.get(k)}
    cond = 'nominal' if meta['scenario'] == 'nominal' else 'disturbed'
    table = BAND if cond == 'nominal' else GUARD
    checks = {}; margins = {}
    for key, (op, thr) in table.items():
        v = rec[key]
        ok = (v <= thr) if op == '<=' else (v >= thr)
        checks[CHECK_NAME[key]] = bool(ok)
        margins[key] = {'value': f(v), 'threshold': thr, 'op': op, 'ratio_value_over_threshold': f(v / thr), 'ok': bool(ok),
                        'value_deg': f(v * DEG) if key == 'orientation_rmse_rad' else None,
                        'threshold_deg': f(thr * DEG) if key == 'orientation_rmse_rad' else None}
    checks['full_cycle'] = bool(len(t) == 31416 and math.isclose(T, 2 * math.pi / 0.1, abs_tol=0)); checks['no_failure'] = not rep['failed']
    snap = next(a for a in SNAP['attempts'] if a['id'] == aid)['evidence']
    snap_checks = snap['nominal_checks'] if cond == 'nominal' else snap['disturbed_checks']
    extra = {
        'attitude_rms_deg': f(rec['orientation_rmse_rad'] * DEG), 'attitude_peak_deg': f(rec['orientation_peak_rad'] * DEG),
        'normal_estimation_rms_deg': f(rec['normal_estimation_rmse_rad'] * DEG),
        'attitude_rms_pre_onset_deg': f(np.sqrt(np.mean(att[t < ONSET] ** 2)) * DEG),
        'attitude_rms_post_release_deg': f(np.sqrt(np.mean(att[t >= RELEASE] ** 2)) * DEG),
        'path_rms_pre_onset_m': f(np.sqrt(np.mean(d['row_path_error_m'][t < ONSET] ** 2))),
        'path_rms_post_release_m': f(np.sqrt(np.mean(d['row_path_error_m'][t >= RELEASE] ** 2))),
        'progress_ratio_pre_onset': f(d['row_actual_progress_m_s'][t < ONSET].sum() / d['row_reference_progress_m_s'][t < ONSET].sum()),
        'progress_ratio_post_release': f(d['row_actual_progress_m_s'][t >= RELEASE].sum() / d['row_reference_progress_m_s'][t >= RELEASE].sum()),
        'saturation_s': f(d['row_saturated'].sum() * C.dt_s), 'qp_intervention_s': f(d['row_qp_intervention'].sum() * C.dt_s),
        'tangent_saturation_s': f((d['res_tangent_saturation_m_s'] > 0).sum() * C.dt_s),
        'normal_saturated_s': f(d['res_normal_saturated'].sum() * C.dt_s),
        'contact_loss_duration_s': f(rec['contact_loss_duration_s']), 'force_overlimit_duration_s': f(rec['force_overlimit_duration_s']),
        'observer_motion_updates': int(d['est_motion_update_applied'].sum()), 'observer_coplanarity_updates': int(d['est_coplanarity_update_applied'].sum()),
        'mean_task_scale': f(d['row_task_scale'].mean()), 'min_task_scale': f(d['row_task_scale'].min()),
        'final_law22_first7': [f(x) for x in meta['final_controller_snapshot_law22']['values'][:7]],
    }
    return {
        'id': aid, 'method': meta['method'], 'condition': cond, 'scenario': meta['scenario'],
        'candidate': meta['identity_payload']['parameters'], 'identity': meta['identity'],
        'recomputed': {k: f(rec[k]) if isinstance(rec[k], (int, float)) else rec[k] for k in ('path_rmse_m', 'progress_ratio', 'orientation_rmse_rad', 'force_mae_n', 'force_peak_n', 'load_min_n', 'force_rmse_n', 'force_error_peak_n', 'path_peak_m', 'orientation_peak_rad', 'normal_estimation_rmse_rad', 'saturation_ticks', 'qp_intervention_ticks', 'residual_path_m', 'actual_progress_m', 'reference_progress_m')},
        'recomputed_vs_artifact_metrics_differences': {k: {'recomputed': f(a), 'artifact': f(b)} for k, (a, b) in diffs.items()},
        'checks_recomputed': checks, 'checks_in_snapshot': snap_checks,
        'checks_match_snapshot': all(checks.get(k) == v for k, v in snap_checks.items()) and set(checks) == set(snap_checks),
        'feasible_recomputed': all(checks.values()),
        'feasible_in_snapshot': snap.get('nominal_feasible', snap.get('disturbed_guards_ok')),
        'failed_checks': sorted(k for k, v in checks.items() if not v),
        'margins': margins, 'descriptors': extra,
        'sha256': meta['_extract']['artifact_sha256'],
    }


def art_for_pair(d, meta, rows):
    return {'method': meta['method'], 'material': meta['material'], 'dt_s': meta['dt_s'], 'duration_s': meta['duration_s'],
            'preparation': meta['preparation'], 'timeline': meta['timeline'], 'identity': meta['identity'],
            'scenario': meta['scenario'], 'metrics': meta['metrics'], 'rows': rows}


def phase_decomposition(dn, dd):
    t = dn['row_time_s']; dt = C.dt_s
    load = np.abs(dd['row_true_normal_load_n'] - dn['row_true_normal_load_n']) / C.jload_scale_n
    path = np.linalg.norm(dd['row_position_m'] - dn['row_position_m'], axis=1) / C.jpath_scale_m
    att = np.abs(dd['row_orientation_error_rad'] - dn['row_orientation_error_rad']) / C.jatt_scale_rad
    w_load = np.asarray([cell_weight(float(x), dt, C.load_onset_s, T, T) for x in t])
    w_path = np.asarray([cell_weight(float(x), dt, C.path_release_s, T, T) for x in t])
    w_att = np.asarray([cell_weight(float(x), dt, C.attitude_onset_s, T, T) for x in t])
    out = {}
    for name, a, b in PHASES:
        m = (t >= a) & (t < b) if name != 'post' else (t >= a)
        out[name] = {'Jload': f((load * w_load)[m].sum()), 'Jpath': f((path * w_path)[m].sum()), 'Jatt': f((att * w_att)[m].sum()),
                     'path_term_unweighted_band_s': f((path * dt)[m].sum()),  # what Jpath would be if the window started here
                     'mean_abs_load_diff_n': f(np.abs(dd['row_true_normal_load_n'] - dn['row_true_normal_load_n'])[m].mean()),
                     'mean_pos_diff_mm': f((np.linalg.norm(dd['row_position_m'] - dn['row_position_m'], axis=1)[m].mean() * 1e3)),
                     'mean_abs_att_diff_deg': f(np.abs(dd['row_orientation_error_rad'] - dn['row_orientation_error_rad'])[m].mean() * DEG)}
    return out


def pair_summary(unit, method, mn, md, dn, dd, metan, metad, rn, rd):
    comp = _objective_components(rn, rd, C)
    snap = next(a for a in SNAP['attempts'] if a['id'] == f'{method}-{unit:02d}-disturbed')['evidence']
    rec = compare_pair(art_for_pair(dn, metan, rn), art_for_pair(dd, metad, rd))
    t = dd['row_time_s']; post = t >= RELEASE
    att_d = dd['row_orientation_error_rad']; att_n = dn['row_orientation_error_rad']
    ld = dd['row_true_normal_load_n']; ln = dn['row_true_normal_load_n']
    hold = (t >= 20.5) & (t < HOLD_END)
    return {
        'unit': unit, 'method': method, 'candidate': metan['identity_payload']['parameters'],
        'J_recomputed': comp, 'J_snapshot': snap['objective_components'],
        'J_abs_diff': f(abs(comp['J'] - snap['objective'])),
        'nominal_feasible': mn['feasible_recomputed'], 'disturbed_guards_ok': md['feasible_recomputed'],
        'pair_feasible_recomputed': bool(mn['feasible_recomputed'] and md['feasible_recomputed']),
        'pair_feasible_snapshot': snap['pair_feasible'], 'nominal_feasible_reported_snapshot': snap['nominal_feasible_reported'],
        'objective_eligible_snapshot': snap['objective_eligible'],
        'phases': phase_decomposition(dn, dd),
        'recovery': {k: (f(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v) for k, v in rec.items()},
        'absolute_disturbed': {
            'force_peak_n': mn and md['recomputed']['force_peak_n'], 'load_min_n': md['recomputed']['load_min_n'],
            'load_min_time_s': f(t[int(np.argmin(ld))]),
            'hold_mean_load_n': f(ld[hold].mean()), 'nominal_hold_mean_load_n': f(ln[hold].mean()),
            'attitude_peak_deg': md['descriptors']['attitude_peak_deg'], 'attitude_rms_deg': md['descriptors']['attitude_rms_deg'],
            'post_release_attitude_rms_deg': md['descriptors']['attitude_rms_post_release_deg'],
            'nominal_post_release_attitude_rms_deg': mn['descriptors']['attitude_rms_post_release_deg'],
            'post_release_attitude_rms_diff_deg': f(md['descriptors']['attitude_rms_post_release_deg'] - mn['descriptors']['attitude_rms_post_release_deg']),
            'progress_ratio': md['recomputed']['progress_ratio'], 'nominal_progress_ratio': mn['recomputed']['progress_ratio'],
            'path_rmse_m': md['recomputed']['path_rmse_m'], 'path_peak_m': md['recomputed']['path_peak_m'],
            'terminal_load_difference_n': f(ld[-1] - ln[-1]),
            'terminal_offset_mm': f(np.linalg.norm(dd['row_position_m'][-1] - dn['row_position_m'][-1]) * 1e3),
            'max_post_release_offset_mm': f(np.linalg.norm(dd['row_position_m'][post] - dn['row_position_m'][post], axis=1).max() * 1e3),
            'contact_loss_duration_s': md['descriptors']['contact_loss_duration_s'], 'force_overlimit_duration_s': md['descriptors']['force_overlimit_duration_s'],
            'saturation_s': md['descriptors']['saturation_s'], 'qp_intervention_s': md['descriptors']['qp_intervention_s'],
        },
        'absolute_nominal': {k: mn['recomputed'][k] for k in ('path_rmse_m', 'progress_ratio', 'orientation_rmse_rad', 'force_mae_n', 'force_peak_n', 'load_min_n')} | {'attitude_rms_deg': mn['descriptors']['attitude_rms_deg'], 'normal_estimation_rms_deg': mn['descriptors']['normal_estimation_rms_deg']},
    }


def binned(d, edges):
    t = d['row_time_s']; out = []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (t >= a) & (t < b)
        lt = d['res_law_tangent_velocity_m_s'][m]; lv = d['res_law_velocity_base_m_s'][m]
        ln = d['res_law_normal_velocity_m_s'][m]; lf = d['res_law_force_base_n'][m]
        nrm = d['est_inward_normal_base'][m]
        # tangential (in-plane w.r.t. estimated normal) law force magnitude
        fn = np.sum(lf * nrm, axis=1, keepdims=True) * nrm; ft = lf - fn
        out.append({'t0': a, 't1': b,
                    'path_rms_mm': f(np.sqrt(np.mean(d['row_path_error_m'][m] ** 2)) * 1e3),
                    'path_peak_mm': f(d['row_path_error_m'][m].max() * 1e3),
                    'progress_ratio': f(d['row_actual_progress_m_s'][m].sum() / max(d['row_reference_progress_m_s'][m].sum(), 1e-12)),
                    'attitude_rms_deg': f(np.sqrt(np.mean(d['row_orientation_error_rad'][m] ** 2)) * DEG),
                    'normal_est_err_rms_deg': f(np.sqrt(np.mean(d['row_normal_estimation_error_rad'][m] ** 2)) * DEG),
                    'load_mean_n': f(d['row_true_normal_load_n'][m].mean()), 'load_min_n': f(d['row_true_normal_load_n'][m].min()), 'load_max_n': f(d['row_true_normal_load_n'][m].max()),
                    'force_error_mae_n': f(np.abs(d['row_force_error_n'][m]).mean()),
                    'law_tangent_speed_rms_mm_s': f(np.sqrt(np.mean(np.sum(lt ** 2, axis=1))) * 1e3),
                    'law_normal_speed_rms_mm_s': f(np.sqrt(np.mean(np.sum(ln ** 2, axis=1))) * 1e3),
                    'law_tangential_force_rms_n': f(np.sqrt(np.mean(np.sum(ft ** 2, axis=1)))),
                    'law_normal_force_rms_n': f(np.sqrt(np.mean(np.sum(fn ** 2, axis=1)))),
                    'tangent_saturation_s': f((d['res_tangent_saturation_m_s'][m] > 0).sum() * C.dt_s),
                    'normal_saturated_s': f(d['res_normal_saturated'][m].sum() * C.dt_s),
                    'qp_intervention_s': f(d['row_qp_intervention'][m].sum() * C.dt_s),
                    'saturated_s': f(d['row_saturated'][m].sum() * C.dt_s),
                    'observer_motion_updates': int(d['est_motion_update_applied'][m].sum()),
                    'mean_task_scale': f(d['row_task_scale'][m].mean()),
                    'w_rms_mm_s': f(np.sqrt(np.mean(np.sum(d['rec_law22'][m][:, 4:7] ** 2, axis=1))) * 1e3)})
    return out


def memory_engagement(dm, dn):
    """MSFC metric eigenvalues from law22 slots 19..21; h slots 7..9; S slots 10..18."""
    t = dm['row_time_s']; lam = dm['rec_law22'][:, 19:22]; lam_n = dn['rec_law22'][:, 19:22]
    assert np.all(lam > 0) and np.all(lam <= 1.0 + 1e-12), 'eigenvalue slots out of (0,1]'
    lmin = lam.min(axis=1); lmin_n = lam_n.min(axis=1)
    h = np.linalg.norm(dm['rec_law22'][:, 7:10], axis=1); S = np.linalg.norm(dm['rec_law22'][:, 10:19], axis=1)
    pre = t < ONSET; act = (t >= ONSET) & (t < 40.0)
    i_min = int(np.argmin(np.where(act, lmin, 2.0)))
    below = act & (lmin < 0.95)
    win = (f(t[below].min()), f(t[below].max())) if below.any() else None
    i_rel = int(np.searchsorted(t, RELEASE)); i_on = int(np.searchsorted(t, ONSET)) - 1
    post = t >= RELEASE
    gap = np.abs(lam - lam_n).max(axis=1)
    below_release = (t >= HOLD_END) & (t < 40.0) & (lmin < 0.99)
    return {
        'lambda_min_before_onset': f(lmin[i_on]), 'lambda_min_nominal_same_tick': f(lmin_n[i_on]),
        'pre_onset_bitwise_equal_to_nominal_law22': bool(np.array_equal(dm['rec_law22'][pre], dn['rec_law22'][pre])),
        'pre_onset_max_abs_law22_diff': f(np.abs(dm['rec_law22'][pre] - dn['rec_law22'][pre]).max()),
        'lambda_min_minimum': f(lmin[i_min]), 'lambda_min_minimum_time_s': f(t[i_min]),
        'window_lambda_min_below_0p95_s': win, 'window_length_s': f(win[1] - win[0] + C.dt_s) if win else 0.0,
        'lambda_min_at_release_35p5': f(lmin[i_rel]), 'gap_to_nominal_at_release': f(gap[i_rel]),
        'max_gap_to_nominal_after_release': f(gap[post].max()),
        'lambda_min_minimum_in_hold_20p5_30p5': f(lmin[(t >= 20.5) & (t < HOLD_END)].min()),
        'lambda_min_minimum_in_release_ramp_30p5_35p5': f(lmin[(t >= HOLD_END) & (t < RELEASE)].min()),
        'release_ramp_time_below_0p99_s': f(below_release.sum() * C.dt_s),
        'h_norm_before_onset': f(h[i_on]), 'h_norm_max_after_onset': f(h[act].max()), 'S_fro_before_onset': f(S[i_on]), 'S_fro_max_after_onset': f(S[act].max()),
        'h_norm_at_release': f(h[i_rel]), 'S_fro_at_release': f(S[i_rel]),
        'seconds_lambda_min_below_0p99_total': f((lmin < 0.99).sum() * C.dt_s),
        'seconds_lambda_min_below_0p99_after_onset': f(((lmin < 0.99) & (t >= ONSET)).sum() * C.dt_s),
    }


def main():
    members = {}; data = {}; metas = {}; rows = {}
    for a in SNAP['attempts']:
        aid = a['id']; d, meta = load(aid); data[aid] = d; metas[aid] = meta; rows[aid] = rows_for_metrics(d)
        members[aid] = member_summary(aid, d, meta, rows[aid])
    pairs = {}
    for a in SNAP['attempts']:
        if a['condition'] != 'disturbed': continue
        m, u = a['method'], a['unit']; n = f'{m}-{u:02d}-nominal'; dd_ = a['id']
        pairs[f'{m}-{u:02d}'] = pair_summary(u, m, members[n], members[dd_], data[n], data[dd_], metas[n], metas[dd_], rows[n], rows[dd_])
    # ---- fixed-triple comparison (units 0..3 complete for all three methods)
    common_units = sorted({p['unit'] for p in pairs.values() if all(f'{m}-{p["unit"]:02d}' in pairs for m in METHODS)})
    desc_keys = {
        'J': lambda p: p['J_recomputed']['J'], 'Jload': lambda p: p['J_recomputed']['Jload'], 'Jpath': lambda p: p['J_recomputed']['Jpath'], 'Jatt': lambda p: p['J_recomputed']['Jatt'],
        'nominal_path_rmse_m': lambda p: p['absolute_nominal']['path_rmse_m'], 'nominal_progress_ratio': lambda p: p['absolute_nominal']['progress_ratio'],
        'nominal_attitude_rms_deg': lambda p: p['absolute_nominal']['attitude_rms_deg'], 'nominal_force_mae_n': lambda p: p['absolute_nominal']['force_mae_n'],
        'nominal_force_peak_n': lambda p: p['absolute_nominal']['force_peak_n'], 'nominal_load_min_n': lambda p: p['absolute_nominal']['load_min_n'],
        'nominal_normal_estimation_rms_deg': lambda p: p['absolute_nominal']['normal_estimation_rms_deg'],
        'disturbed_force_peak_n': lambda p: p['absolute_disturbed']['force_peak_n'], 'disturbed_load_min_n': lambda p: p['absolute_disturbed']['load_min_n'],
        'disturbed_attitude_peak_deg': lambda p: p['absolute_disturbed']['attitude_peak_deg'], 'post_release_attitude_rms_deg': lambda p: p['absolute_disturbed']['post_release_attitude_rms_deg'],
        'disturbed_progress_ratio': lambda p: p['absolute_disturbed']['progress_ratio'], 'terminal_offset_mm': lambda p: p['absolute_disturbed']['terminal_offset_mm'],
        'terminal_load_difference_n': lambda p: p['absolute_disturbed']['terminal_load_difference_n'], 'recovery_s': lambda p: p['recovery']['recovery_s'],
        'yield_peak_mm': lambda p: p['recovery']['yield_peak_m'] * 1e3 if p['recovery'].get('yield_peak_m') is not None else None,
        'hold_mean_load_n': lambda p: p['absolute_disturbed']['hold_mean_load_n'],
        'max_post_release_offset_mm': lambda p: p['absolute_disturbed']['max_post_release_offset_mm'],
        'disturbed_load_min_time_s': lambda p: p['absolute_disturbed']['load_min_time_s'],
        'disturbed_qp_intervention_s': lambda p: p['absolute_disturbed']['qp_intervention_s'],
        'disturbed_saturation_s': lambda p: p['absolute_disturbed']['saturation_s'],
    }
    table = {}
    for k, fn in desc_keys.items():
        table[k] = {m: {u: f(fn(pairs[f'{m}-{u:02d}'])) for u in common_units} for m in METHODS}
    comparisons = {}
    for A, B in (('SFC', 'DSFC'), ('SFC', 'MSFC'), ('DSFC', 'MSFC')):
        comp = {}
        for k in desc_keys:
            diffs = {}
            for u in common_units:
                a, b = table[k][A][u], table[k][B][u]
                diffs[u] = None if a is None or b is None else f(b - a)
            signs = [None if v is None else (1 if v > 0 else (-1 if v < 0 else 0)) for v in diffs.values()]
            nz = [s for s in signs if s not in (None, 0)]
            feas_units = [u for u in common_units if pairs[f'{A}-{u:02d}']['pair_feasible_recomputed'] and pairs[f'{B}-{u:02d}']['pair_feasible_recomputed']]
            nz_feas = [signs[common_units.index(u)] for u in feas_units if signs[common_units.index(u)] not in (None, 0)]
            comp[k] = {'B_minus_A_by_unit': diffs, 'signs': signs,
                       'sign_stable_all_common_units': bool(nz and all(s == nz[0] for s in nz) and None not in signs),
                       'sign_stable_on_both_pair_feasible_units': bool(nz_feas and all(s == nz_feas[0] for s in nz_feas)),
                       'both_pair_feasible_units': feas_units,
                       'min_abs': f(min(abs(v) for v in diffs.values() if v is not None)) if any(v is not None for v in diffs.values()) else None,
                       'max_abs': f(max(abs(v) for v in diffs.values() if v is not None)) if any(v is not None for v in diffs.values()) else None}
        comparisons[f'{B}_minus_{A}'] = comp
    # ---- Q3 mechanics: unit 2 (and unit 3 for contrast) nominal + disturbed, binned
    edges = [0, 5, 10, 15, 20, 20.5, 25, 30.5, 35.5, 40, 50, T]
    mechanics = {}
    for u in (0, 2, 3):
        for m in METHODS:
            for cond in ('nominal', 'disturbed'):
                aid = f'{m}-{u:02d}-{cond}'
                if aid in data: mechanics[aid] = binned(data[aid], edges)
    # law tangent velocity vs progress: SFC-02 nominal vs DSFC-02 nominal, whole path
    def tangent_stats(aid):
        d = data[aid]; t = d['row_time_s']
        lt = d['res_law_tangent_velocity_m_s']; ref = d['res_reference_velocity_base_m_s']
        # component of law tangent velocity along the reference velocity direction (negative = opposing progress)
        rn = np.linalg.norm(ref, axis=1); ok = rn > 1e-9
        along = np.full(len(t), np.nan); along[ok] = np.sum(lt[ok] * ref[ok], axis=1) / rn[ok]
        lat = np.full(len(t), np.nan); lat[ok] = np.sqrt(np.maximum(np.sum(lt[ok] ** 2, axis=1) - along[ok] ** 2, 0))
        cmd = d['res_commanded_tangent_progress_m_s']; planned = d['res_planned_progress_m_s']
        return {'law_tangent_along_reference_mean_mm_s': f(np.nanmean(along) * 1e3), 'law_tangent_along_reference_min_mm_s': f(np.nanmin(along) * 1e3),
                'law_tangent_lateral_rms_mm_s': f(np.sqrt(np.nanmean(lat ** 2)) * 1e3),
                'reference_speed_mean_mm_s': f(rn.mean() * 1e3),
                'commanded_tangent_progress_mean_mm_s': f(np.nanmean(cmd) * 1e3), 'planned_progress_mean_mm_s': f(np.nanmean(planned) * 1e3),
                'measured_progress_mean_mm_s': f(np.nanmean(d['res_measured_progress_m_s']) * 1e3),
                'actual_progress_mean_mm_s': f(d['row_actual_progress_m_s'].mean() * 1e3), 'reference_progress_mean_mm_s': f(d['row_reference_progress_m_s'].mean() * 1e3),
                'ticks_actual_progress_negative': int((d['row_actual_progress_m_s'] < 0).sum()),
                'seconds_actual_progress_below_half_reference': f(((d['row_actual_progress_m_s'] < 0.5 * d['row_reference_progress_m_s']) & (d['row_reference_progress_m_s'] > 1e-6)).sum() * C.dt_s),
                'tangent_speed_cap_m_s': metas[aid]['identity_payload']['settings']['tangent_speed_cap_m_s'],
                'normal_speed_cap_m_s': metas[aid]['identity_payload']['settings']['normal_speed_cap_m_s'],
                'w_rms_mm_s': f(np.sqrt(np.mean(np.sum(d['rec_law22'][:, 4:7] ** 2, axis=1))) * 1e3),
                'w_max_mm_s': f(np.sqrt(np.sum(d['rec_law22'][:, 4:7] ** 2, axis=1)).max() * 1e3),
                'g_times_w_rms_mm_s': f(metas[aid]['identity_payload']['parameters']['g'] * np.sqrt(np.mean(np.sum(d['rec_law22'][:, 4:7] ** 2, axis=1))) * 1e3),
                'filtered_lateral_force_rms_n': f(np.sqrt(np.mean(np.sum((d['res_filtered_force_base_n'] - np.sum(d['res_filtered_force_base_n'] * d['est_inward_normal_base'], axis=1, keepdims=True) * d['est_inward_normal_base']) ** 2, axis=1)))),
                'normal_estimation_rms_deg': f(np.sqrt(np.mean(d['row_normal_estimation_error_rad'] ** 2)) * DEG),
                'orientation_rms_deg': f(np.sqrt(np.mean(d['row_orientation_error_rad'] ** 2)) * DEG)}
    tangent = {aid: tangent_stats(aid) for aid in data if aid.endswith('nominal')}
    # ---- entry gate arithmetic
    gate = {}
    for m in METHODS:
        units = sorted(int(a['unit']) for a in SNAP['attempts'] if a['method'] == m)
        decided = {}
        for u in units:
            n = members.get(f'{m}-{u:02d}-nominal'); d_ = members.get(f'{m}-{u:02d}-disturbed')
            if n is None: continue
            if not n['feasible_recomputed']: decided[u] = 'infeasible_nominal'
            elif d_ is None: decided[u] = 'nominal_feasible_disturbed_pending'
            else: decided[u] = 'pair_feasible' if d_['feasible_recomputed'] else 'infeasible_disturbed_guard'
        feas = sum(1 for v in decided.values() if v == 'pair_feasible')
        undecided = [u for u in range(8) if u not in decided or decided[u] == 'nominal_feasible_disturbed_pending']
        gate[m] = {'decided_units': decided, 'pair_feasible_so_far': feas, 'initial_units_undecided': undecided,
                   'needed_from_undecided_to_reach_3': max(0, 3 - feas), 'can_still_reach_3': feas + len(undecided) >= 3,
                   'already_meets_gate': feas >= 3}
    from yield_contact_tuner import YieldContactTuner
    tuner = YieldContactTuner(training_cell_id='stiff_low_mu', selection_contract_id='yield-fair-selection-contract-v1')
    triples = [{'unit': i, 'm': m_, 'mu': mu_, 'g': g_, 'mu_g': m_ and mu_ * g_, 'g_over_m': g_ / m_, 'mu_over_m': mu_ / m_} for i, (m_, mu_, g_) in enumerate(tuner.shared_mechanical_triples())]
    # ---- MSFC memory engagement on the training cell
    mem = {}
    for u in (0, 1, 2, 3):
        mem[f'MSFC-{u:02d}'] = {'disturbed': memory_engagement(data[f'MSFC-{u:02d}-disturbed'], data[f'MSFC-{u:02d}-nominal'])}
        dn = data[f'MSFC-{u:02d}-nominal']; lam = dn['rec_law22'][:, 19:22].min(axis=1); t = dn['row_time_s']
        mem[f'MSFC-{u:02d}']['nominal'] = {'lambda_min_minimum_whole_path': f(lam.min()), 'time_s': f(t[int(np.argmin(lam))]), 'lambda_min_at_19p998': f(lam[int(np.searchsorted(t, ONSET)) - 1]),
                                            'seconds_below_0p99': f((lam < 0.99).sum() * C.dt_s), 'seconds_below_0p95': f((lam < 0.95).sum() * C.dt_s)}
        mem[f'MSFC-{u:02d}']['J_MSFC_minus_DSFC_same_triple'] = {k: f(pairs[f'MSFC-{u:02d}']['J_recomputed'][k] - pairs[f'DSFC-{u:02d}']['J_recomputed'][k]) for k in ('J', 'Jload', 'Jpath', 'Jatt')}
        mem[f'MSFC-{u:02d}']['phase_J_MSFC_minus_DSFC'] = {ph: {k: f(pairs[f'MSFC-{u:02d}']['phases'][ph][k] - pairs[f'DSFC-{u:02d}']['phases'][ph][k]) for k in ('Jload', 'Jpath', 'Jatt')} for ph, _, _ in PHASES}
    # nominal-member law22 identity between DSFC and MSFC? (structure only; MSFC metric not exactly identity)
    out = {
        'scope': 'round-8 advisory post-processing of the fixed 25-attempt training snapshot; development data; no run; no formal unit; training unchanged',
        'contract': {'bands': {k: v[1] for k, v in BAND.items()}, 'attitude_band_deg': C.nominal_attitude_rms_rad_max * DEG, 'guards': {k: v[1] for k, v in GUARD.items()},
                     'scales': {'Jload_n': C.jload_scale_n, 'Jpath_m': C.jpath_scale_m, 'Jatt_rad': C.jatt_scale_rad}, 'windows': {'load_onset_s': C.load_onset_s, 'path_release_s': C.path_release_s, 'attitude_onset_s': C.attitude_onset_s, 'period_s': T}},
        'all_recomputed_checks_match_snapshot': all(m['checks_match_snapshot'] for m in members.values()),
        'all_recomputed_J_match_snapshot_to_1e-9': all(p['J_abs_diff'] < 1e-9 for p in pairs.values()),
        'members': members, 'pairs': pairs,
        'fixed_triple_table': {'common_units': common_units, 'values': table, 'comparisons': comparisons},
        'unit_mechanics_binned': {'edges_s': edges, 'series': mechanics},
        'nominal_tangent_stats': tangent,
        'entry_gate': gate, 'shared_initial_triples': triples,
        'msfc_memory_engagement': mem,
    }
    print(json.dumps(out, indent=1, sort_keys=False, allow_nan=False))


if __name__ == '__main__':
    main()
