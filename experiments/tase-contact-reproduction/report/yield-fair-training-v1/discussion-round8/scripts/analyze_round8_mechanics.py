"""Round-8 mechanics supplement: fine bins over the early episode, law velocity/force
alignment (per-axis vs radial law), disturbed progress-guard inflation by the tangential
push, load-peak timing, and path-clock/QP scaling counts.  Reads /tmp/yfp8 caches only."""
import json, math, sys
from pathlib import Path
import numpy as np
CACHE = Path('/tmp/yfp8'); DEG = 180 / math.pi; DT = 0.002
SNAP = json.loads((Path(__file__).resolve().parents[4] / 'report/yield-fair-training-v1/round8-input-snapshot.json').read_text())
IDS = [a['id'] for a in SNAP['attempts']]
def f(x): return None if x is None else float(x)
def load(aid):
    z = np.load(CACHE / f'{aid}.npz'); return {k: z[k] for k in z.files}
D = {aid: load(aid) for aid in IDS}
out = {}
# 1) fine 1 s bins 0-14 s, unit 0/2 nominal, all methods
def fine(d, edges):
    t = d['row_time_s']; res = []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (t >= a) & (t < b); lf = d['res_law_force_base_n'][m]; nrm = d['est_inward_normal_base'][m]
        fn = np.sum(lf * nrm, axis=1, keepdims=True) * nrm; ft = lf - fn
        res.append({'t0': a, 't1': b, 'load_min': f(d['row_true_normal_load_n'][m].min()), 'load_max': f(d['row_true_normal_load_n'][m].max()),
                    'force_err_mae': f(np.abs(d['row_force_error_n'][m]).mean()), 'path_rms_mm': f(np.sqrt(np.mean(d['row_path_error_m'][m] ** 2)) * 1e3),
                    'progress': f(d['row_actual_progress_m_s'][m].sum() / max(d['row_reference_progress_m_s'][m].sum(), 1e-12)),
                    'att_rms_deg': f(np.sqrt(np.mean(d['row_orientation_error_rad'][m] ** 2)) * DEG), 'nest_rms_deg': f(np.sqrt(np.mean(d['row_normal_estimation_error_rad'][m] ** 2)) * DEG),
                    'nest_end_deg': f(d['row_normal_estimation_error_rad'][m][-1] * DEG),
                    'motion_updates': int(d['est_motion_update_applied'][m].sum()), 'coplanarity_updates': int(d['est_coplanarity_update_applied'][m].sum()),
                    'excitation_gate_ticks': int(d['est_excitation_gate'][m].sum()), 'contact_gate_ticks': int(d['est_contact_gate'][m].sum()),
                    'law_tangential_force_rms_n': f(np.sqrt(np.mean(np.sum(ft ** 2, axis=1)))), 'law_normal_force_rms_n': f(np.sqrt(np.mean(np.sum(fn ** 2, axis=1)))),
                    'law_speed_rms_mm_s': f(np.sqrt(np.mean(np.sum(d['res_law_velocity_base_m_s'][m] ** 2, axis=1))) * 1e3),
                    'ref_speed_mean_mm_s': f(np.linalg.norm(d['res_reference_velocity_base_m_s'][m], axis=1).mean() * 1e3),
                    'lateral_filtered_force_rms_n': f(np.sqrt(np.mean(np.sum((d['res_filtered_force_base_n'][m] - np.sum(d['res_filtered_force_base_n'][m] * nrm, axis=1, keepdims=True) * nrm) ** 2, axis=1))))})
    return res
edges = list(range(0, 15)) + [20]
out['fine_bins_0_20s'] = {aid: fine(D[aid], edges) for aid in IDS if aid.endswith('nominal') and aid.split('-')[1] in ('00', '02', '03', '04')}
# 2) law velocity vs law force alignment (angle) per method/unit, nominal, |F|>0.05 N
def alignment(d):
    v = d['res_law_velocity_base_m_s']; F = d['res_law_force_base_n']; nv = np.linalg.norm(v, axis=1); nF = np.linalg.norm(F, axis=1)
    ok = (nF > 0.05) & (nv > 1e-6)
    c = np.sum(v[ok] * F[ok], axis=1) / (nv[ok] * nF[ok]); ang = np.degrees(np.arccos(np.clip(c, -1, 1)))
    # per-axis anisotropy measure: ratio of law speed to g*|w|? skip; report angle stats
    return {'ticks_used': int(ok.sum()), 'angle_rms_deg': f(np.sqrt(np.mean(ang ** 2))), 'angle_median_deg': f(np.median(ang)), 'angle_p90_deg': f(np.percentile(ang, 90)), 'angle_max_deg': f(ang.max())}
out['law_velocity_force_alignment_nominal'] = {aid: alignment(D[aid]) for aid in IDS if aid.endswith('nominal')}
out['law_velocity_force_alignment_disturbed_hold'] = {}
for aid in IDS:
    if aid.endswith('disturbed'):
        d = D[aid]; t = d['row_time_s']; m = (t >= 20.5) & (t < 30.5)
        dd = {k: v[m] for k, v in d.items() if k.startswith('res_') and v.ndim == 2}; dd.update({k: v[m] for k, v in d.items() if k.startswith('res_') and v.ndim == 1})
        out['law_velocity_force_alignment_disturbed_hold'][aid] = alignment(dd)
# 3) disturbed progress guard inflation
prog = {}
for aid in IDS:
    if not aid.endswith('disturbed'): continue
    n = aid.replace('disturbed', 'nominal'); dn, dd = D[n], D[aid]; t = dd['row_time_s']
    win = (t >= 20.0) & (t < 35.5); outside = ~win
    ref = dd['row_reference_progress_m_s']
    prog[aid[:-10]] = {
        'nominal_whole': f(dn['row_actual_progress_m_s'].sum() / dn['row_reference_progress_m_s'].sum()),
        'disturbed_whole': f(dd['row_actual_progress_m_s'].sum() / ref.sum()),
        'disturbed_in_window_20_35p5': f(dd['row_actual_progress_m_s'][win].sum() / ref[win].sum()),
        'nominal_in_window_20_35p5': f(dn['row_actual_progress_m_s'][win].sum() / dn['row_reference_progress_m_s'][win].sum()),
        'disturbed_outside_window': f(dd['row_actual_progress_m_s'][outside].sum() / ref[outside].sum()),
        'nominal_outside_window': f(dn['row_actual_progress_m_s'][outside].sum() / dn['row_reference_progress_m_s'][outside].sum()),
        'disturbed_whole_if_window_replaced_by_nominal': f((dd['row_actual_progress_m_s'][outside].sum() + dn['row_actual_progress_m_s'][win].sum()) / ref.sum()),
        'push_20_20p5_ratio': f(dd['row_actual_progress_m_s'][(t >= 20) & (t < 20.5)].sum() / ref[(t >= 20) & (t < 20.5)].sum()),
        'hold_20p5_30p5_ratio': f(dd['row_actual_progress_m_s'][(t >= 20.5) & (t < 30.5)].sum() / ref[(t >= 20.5) & (t < 30.5)].sum()),
        'release_30p5_35p5_ratio': f(dd['row_actual_progress_m_s'][(t >= 30.5) & (t < 35.5)].sum() / ref[(t >= 30.5) & (t < 35.5)].sum()),
        'guard_0p85_passes_whole': bool(dd['row_actual_progress_m_s'].sum() / ref.sum() >= 0.85),
        'guard_would_pass_outside_window_only': bool(dd['row_actual_progress_m_s'][outside].sum() / ref[outside].sum() >= 0.85),
        'path_clock_frozen_ticks': int(dd['res_path_clock_frozen'].sum()), 'task_scale_below_1_ticks': int((dd['row_task_scale'] < 1 - 1e-12).sum()),
        'task_scale_min': f(dd['row_task_scale'].min()), 'reference_progress_total_m': f(ref.sum() * DT), 'nominal_reference_progress_total_m': f(dn['row_reference_progress_m_s'].sum() * DT)}
out['disturbed_progress_guard'] = prog
# 4) load peak timing and value per member
peaks = {}
for aid in IDS:
    d = D[aid]; L = d['row_true_normal_load_n']; t = d['row_time_s']; i = int(np.argmax(L))
    peaks[aid] = {'peak_n': f(L[i]), 'peak_time_s': f(t[i]), 'peak_before_onset': bool(t[i] < 20.0), 'max_load_20_to_end_n': f(L[t >= 20].max()), 'min_load_n': f(L.min()), 'min_load_time_s': f(t[int(np.argmin(L))])}
out['load_peak_timing'] = peaks
# 5) SFC-01 recovery=0 diagnostics + all pairs distance at release
rel = {}
for aid in IDS:
    if not aid.endswith('disturbed'): continue
    n = aid.replace('disturbed', 'nominal'); dn, dd = D[n], D[aid]; t = dd['row_time_s']; i = int(np.searchsorted(t, 35.5))
    dist = np.linalg.norm(dd['row_position_m'] - dn['row_position_m'], axis=1); fdiff = np.abs(dd['row_true_normal_load_n'] - dn['row_true_normal_load_n'])
    post = t >= 35.5
    rel[aid[:-10]] = {'distance_at_release_mm': f(dist[i] * 1e3), 'load_diff_at_release_n': f(fdiff[i]), 'max_post_release_distance_mm': f(dist[post].max() * 1e3), 'max_post_release_load_diff_n': f(fdiff[post].max()),
                      'time_of_max_post_distance_s': f(t[post][int(np.argmax(dist[post]))]), 'seconds_post_release_distance_above_2mm': f((dist[post] > 0.002).sum() * DT), 'seconds_post_release_load_diff_above_0p5N': f((fdiff[post] > 0.5).sum() * DT),
                      'mean_post_release_distance_mm': f(dist[post].mean() * 1e3), 'mean_post_release_load_diff_n': f(fdiff[post].mean())}
out['release_diagnostics'] = rel
# 6) whole-path attitude vs normal-estimation identity check
out['attitude_equals_normal_estimate_error'] = {aid: {'orientation_rms_deg': f(np.sqrt(np.mean(D[aid]['row_orientation_error_rad'] ** 2)) * DEG), 'normal_est_rms_deg': f(np.sqrt(np.mean(D[aid]['row_normal_estimation_error_rad'] ** 2)) * DEG), 'rms_of_difference_deg': f(np.sqrt(np.mean((D[aid]['row_orientation_error_rad'] - D[aid]['row_normal_estimation_error_rad']) ** 2)) * DEG)} for aid in IDS}
print(json.dumps(out, indent=1, allow_nan=False))
