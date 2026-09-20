"""Round 4 bounded analysis of NO-v3 receipts (read-only postprocessing).

Inputs: /tmp/ynv3/<cell>.npz caches from extract_ticks_v3.py.
Output: JSON to stdout.  Evaluator truth (true normal, true load, external
force) is used only for analysis; nothing here feeds a controller.
"""
import json, sys, math
import numpy as np
sys.path.insert(0, 'tools')
from contact_yield_simulator import SurfaceField

DT = 0.002; KP = 120.0; TARGET = 5.0; MU = 0.15
START, RISE_END, HOLD_END, REL = 20.0, 20.5, 30.5, 35.5
PATH_BAND = 0.002; FORCE_BAND = 0.5; GATE = 0.002; CAP = 0.05
LATERAL = [(11.96, 19.45), (43.38, 50.87)]
CACHE = '/tmp/ynv3/'

def L(name): return np.load(CACHE + name + '.npz')
def unit(a): return a / np.maximum(np.linalg.norm(a, axis=-1, keepdims=True), 1e-15)
def ang(a, b):
    c = np.sum(unit(a) * unit(b), axis=-1); return np.degrees(np.arccos(np.clip(c, -1, 1)))
def proj(n, x):  # tangent projection of x w.r.t. unit normals n (row-wise)
    return x - np.sum(n * x, axis=-1, keepdims=True) * n
def rms(a): a = np.asarray(a, float); a = a[np.isfinite(a)]; return float(np.sqrt(np.mean(a * a))) if a.size else None
def r(x, k=3): return None if x is None else (float(round(float(x), k)) if np.isfinite(x) else None)
def frame(n_true, ref_vel):
    d = unit(proj(n_true, ref_vel)); b = np.cross(n_true, d); return d, b
def components_deg(n_est, n_true, d, b):
    return np.degrees(np.arcsin(np.clip(np.sum(n_est * d, -1), -1, 1))), np.degrees(np.arcsin(np.clip(np.sum(n_est * b, -1), -1, 1)))
def gated(D): return D['contact_gate'] & D['excitation_gate']
def tspeed_est(D): return np.linalg.norm(proj(D['n_est'], D['v']), axis=1)

out = {'clock': 'PATH clock from rows (t); observation clock = t + 1 s entry', 'dt_s': DT}

# ---------- A. recovery decomposition ----------
A = {}
for nom_name, cells in [('combined-mild-approach', ['combined-normal-hold', 'combined-tangent-hold']),
                        ('retained-approach', ['frozen-normal-hold', 'frozen-tangent-hold'])]:
    N = L(nom_name)
    for cn in cells:
        Dd = L(cn); t = Dd['t']; assert np.allclose(t, N['t'], atol=1e-9)
        dist = np.linalg.norm(Dd['pos'] - N['pos'], axis=1); dload = Dd['load_true'] - N['load_true']
        dest = ang(Dd['n_est'], N['n_est'])
        post = t >= REL; active = (t >= START) & (t < REL)
        def first_hold(good):
            suf = np.logical_and.accumulate(good[::-1])[::-1]
            c = np.where(post & suf & (t <= t[-1] - 0.1))[0]; return (float(t[c[0]] - REL) if len(c) else None)
        gp = dist <= PATH_BAND; gf = np.abs(dload) <= FORCE_BAND
        rec = first_hold(gp & gf); recp = first_hold(gp); recf = first_hold(gf)
        i_rel = int(np.argmin(np.abs(t - REL)))
        i_rec = int(np.argmin(np.abs(t - (REL + rec)))) if rec is not None else None
        # dist vs estimate divergence after release
        w = post & (dest > 0.5) & (t < REL + (rec if rec else 10) + 2)
        slope = None
        if w.sum() > 50:
            slope = float(np.polyfit(dest[w], 1e3 * dist[w], 1)[0])
        bins = []
        for a in list(np.arange(19, 36, 1.0)) + list(np.arange(36, 63, 2.0)):
            m = (t >= a) & (t < a + (1.0 if a < 36 else 2.0))
            bins.append({'path_t': float(a), 'dist_mm': r(1e3 * dist[m].mean()), 'dload_n': r(dload[m].mean()),
                         'abs_dload_max_n': r(np.abs(dload[m]).max()), 'est_vs_nom_deg': r(dest[m].mean()),
                         'n_err_deg': r(np.degrees(Dd['n_err'][m]).mean()), 'n_err_nom_deg': r(np.degrees(N['n_err'][m]).mean()),
                         'load_true_n': r(Dd['load_true'][m].mean()), 'f_ext_n': r(np.linalg.norm(Dd['f_ext'][m], axis=1).mean()),
                         'gate_open': r(gated(Dd)[m].mean()), 'tangent_speed_est_mm_s': r(1e3 * np.median(tspeed_est(Dd)[m])),
                         'measured_progress_mm_s': r(1e3 * Dd['act_progress'][m].mean()), 'ref_speed_mm_s': r(1e3 * Dd['ref_progress'][m].mean())})
        A[cn] = {'matched_nominal': nom_name, 'recovery_s_both_bands': r(rec), 'recovery_s_path_band_only': r(recp),
                 'recovery_s_force_band_only': r(recf), 'binding_band': ('path' if (recp or 0) >= (recf or 0) else 'force'),
                 'yield_peak_mm': r(1e3 * dist[active].max()), 'post_release_max_dist_mm': r(1e3 * dist[post].max()),
                 'post_release_max_abs_dload_n': r(np.abs(dload[post]).max()),
                 'post_release_abs_dload_max_time_path_s': r(t[post][np.argmax(np.abs(dload[post]))]),
                 'est_vs_nominal_deg_at_release': r(dest[i_rel]), 'est_vs_nominal_deg_at_recovery': (r(dest[i_rec]) if i_rec is not None else None),
                 'est_vs_nominal_deg_max_post_release': r(dest[post].max()),
                 'dist_vs_est_divergence_slope_mm_per_deg': r(slope), 'leak_only_prediction_mm_per_deg': r(1e3 * TARGET / KP * math.pi / 180),
                 'bins': bins}
out['A_recovery'] = A

# ---------- B. tangent push geometry and coplanarity ----------
B = {}
for cn in ['combined-tangent-hold', 'frozen-tangent-hold']:
    D = L(cn); t = D['t']; n = D['n_true']; v = D['v']; fe = D['f_ext']; ff = D['f_filt']; ne = D['n_est']
    hold = (t >= RISE_END) & (t < HOLD_END); g = gated(D)
    vt = proj(n, v); rt = proj(n, D['ref_vel'])
    th = ang(fe, vt); th_ref = ang(fe, rt); th_v_ref = ang(vt, rt)
    c = np.cross(ff, v); cn_ = np.linalg.norm(c, axis=1); ok = cn_ > 1e-9
    r_true = np.sum(n * c, -1) / np.maximum(cn_, 1e-15)
    r_ext = np.sum(n * np.cross(fe, v), -1) / np.maximum(cn_, 1e-15)
    r_con = np.sum(n * np.cross(ff - fe, v), -1) / np.maximum(cn_, 1e-15)
    fc_true = D['f_true_post'] - fe
    r_con_true = np.sum(n * np.cross(fc_true, v), -1) / np.maximum(np.linalg.norm(np.cross(fc_true + fe, v), axis=1), 1e-15)
    # closed form: n.(f_ext x v_t) = |f_ext||v_t| sin(theta_signed); |f x v| ~ N |v_t|
    sgn = np.sign(np.sum(n * np.cross(fe, vt), -1))
    r_pred = np.linalg.norm(fe, axis=1) * np.sin(np.radians(th)) * sgn / np.maximum(D['load_true'], 1e-6)
    r_est = D['cp_residual'] if np.isfinite(D['cp_residual']).any() else np.sum(ne * c, -1) / np.maximum(cn_, 1e-15)
    m = hold & g & ok
    # estimate motion during hold
    dn = np.linalg.norm(np.diff(ne, axis=0), axis=1) / DT; dn = np.concatenate([[0.0], dn])
    d, b = frame(n, D['ref_vel']); e_al, e_ac = components_deg(ne, n, d, b)
    bpush = np.cross(n, unit(fe));
    tilt_push_binormal = np.degrees(np.arcsin(np.clip(np.sum(ne * bpush, -1), -1, 1)))
    tilt_push_dir = np.degrees(np.arcsin(np.clip(np.sum(ne * unit(fe), -1), -1, 1)))
    series = []
    for a in np.arange(19, 47, 1.0):
        mm = (t >= a) & (t < a + 1)
        series.append({'path_t': float(a), 'theta_push_vs_vt_deg_median': r(np.median(th[mm])), 'r_true_rms': r(rms(r_true[mm & ok])),
                       'r_est_rms': r(rms(r_est[mm & ok])), 'err_along_ref_deg': r(e_al[mm].mean()), 'err_across_ref_deg': r(e_ac[mm].mean()),
                       'tilt_along_push_deg': r(tilt_push_dir[mm].mean()), 'tilt_push_binormal_deg': r(tilt_push_binormal[mm].mean()),
                       'est_rate_deg_s_median': r(np.degrees(np.median(dn[mm]))), 'gate_open': r(g[mm].mean()),
                       'tangent_speed_est_mm_s_median': r(1e3 * np.median(tspeed_est(D)[mm])), 'measured_progress_mm_s': r(1e3 * D['act_progress'][mm].mean())})
    B[cn] = {'hold_window_path_s': [RISE_END, HOLD_END], 'gated_ticks_in_hold': int(m.sum()),
             'theta_push_vs_actual_vt_deg': {'p10': r(np.percentile(th[hold], 10)), 'median': r(np.median(th[hold])), 'p90': r(np.percentile(th[hold], 90))},
             'theta_push_vs_ref_tangent_deg_max': r(th_ref[hold].max()), 'theta_actual_vt_vs_ref_deg_median': r(np.median(th_v_ref[hold])),
             'cp_residual_true_normal_rms_hold': r(rms(r_true[m])), 'cp_residual_external_part_rms_hold': r(rms(r_ext[m])),
             'cp_residual_contact_part_rms_hold_filtered': r(rms(r_con[m])), 'cp_residual_contact_part_rms_hold_true_post': r(rms(r_con_true[m])),
             'cp_residual_closed_form_rms_hold': r(rms(r_pred[m])), 'cp_residual_closed_form_vs_external_corr': r(np.corrcoef(r_pred[m], r_ext[m])[0, 1]),
             'cp_residual_estimate_rms_hold': r(rms(r_est[m])), 'cp_residual_true_normal_rms_nominal_window_0_20': r(rms(r_true[(t < START) & g & ok])),
             'load_true_hold_mean_n': r(D['load_true'][hold].mean()), 'f_ext_hold_mean_n': r(np.linalg.norm(fe[hold], axis=1).mean()),
             'tangential_force_est_frame_hold_rms_n': r(rms(np.linalg.norm(proj(ne, ff), axis=1)[hold])),
             'friction_bound_mu_N_hold_mean_n': r((MU * D['load_true'][hold]).mean()),
             'est_rate_at_cap_fraction_hold_gated': r(np.mean(dn[m] >= 0.98 * CAP)), 'est_rate_max_deg_s': r(np.degrees(dn.max())),
             'n_err_max_deg': r(np.degrees(D['n_err']).max()), 'n_err_max_time_path_s': r(t[np.argmax(D['n_err'])]),
             'series_1s': series}
out['B_tangent'] = B

# ---------- C. normal push: energy ratio, post-release load excursion ----------
C = {}
for cn in ['combined-normal-hold', 'frozen-normal-hold', 'combined-mild-approach', 'retained-approach']:
    D = L(cn); t = D['t']; n = D['n_true']; ne = D['n_est']; v = D['v']; g = gated(D)
    an_est = np.sum(ne * v, -1); pt_est = np.linalg.norm(proj(ne, v), axis=1)
    an_tru = np.sum(n * v, -1); pt_tru = np.linalg.norm(proj(n, v), axis=1)
    dn = np.concatenate([[0.0], np.linalg.norm(np.diff(ne, axis=0), axis=1) / DT])
    win = []
    for a in np.arange(18, 46, 2.0):
        mm = (t >= a) & (t < a + 2)
        win.append({'path_t': float(a), 'energy_ratio_est_frame': r(np.sum(an_est[mm] ** 2) / max(np.sum(pt_est[mm] ** 2), 1e-18)),
                    'energy_ratio_true_frame': r(np.sum(an_tru[mm] ** 2) / max(np.sum(pt_tru[mm] ** 2), 1e-18)),
                    'normal_speed_true_rms_mm_s': r(1e3 * rms(an_tru[mm])), 'tangent_speed_true_rms_mm_s': r(1e3 * rms(pt_tru[mm])),
                    'gate_open': r(g[mm].mean()), 'load_true_mean_n': r(D['load_true'][mm].mean()), 'meas_force_norm_mean_n': r(np.linalg.norm(D['f_filt'][mm], axis=1).mean()),
                    'n_err_deg': r(np.degrees(D['n_err'][mm]).mean()), 'est_rate_deg_s_rms': r(np.degrees(rms(dn[mm])))})
    C[cn] = {'windows_2s': win}
    if 'hold' in cn:
        nomn = 'combined-mild-approach' if cn.startswith('combined') else 'retained-approach'
        N = L(nomn); dload = D['load_true'] - N['load_true']; post = t >= REL
        # fine post-release excursion
        i = np.where(post & (np.abs(dload) > FORCE_BAND))[0]
        C[cn]['post_release_force_band_violations'] = {'n_ticks': int(len(i)), 'first_path_s': (r(t[i[0]]) if len(i) else None), 'last_path_s': (r(t[i[-1]]) if len(i) else None),
                                                      'max_abs_dload_n': r(np.abs(dload[post]).max()), 'at_path_s': r(t[post][np.argmax(np.abs(dload[post]))])}
        fine = []
        for a in np.arange(35.0, 41.0, 0.5):
            mm = (t >= a) & (t < a + 0.5)
            fine.append({'path_t': float(a), 'dload_mean_n': r(dload[mm].mean()), 'dload_max_abs_n': r(np.abs(dload[mm]).max()), 'load_true_n': r(D['load_true'][mm].mean()),
                         'load_nom_n': r(N['load_true'][mm].mean()), 'est_vs_nom_deg': r(ang(ne[mm], N['n_est'][mm]).mean()), 'est_rate_deg_s_rms': r(np.degrees(rms(dn[mm]))),
                         'normal_speed_cmd_mm_s_rms': r(1e3 * rms(D['normal_speed'][mm])), 'normal_speed_cmd_nom_mm_s_rms': r(1e3 * rms(N['normal_speed'][mm]))})
        C[cn]['post_release_fine_0p5s'] = fine
out['C_normal'] = C

# ---------- D. priors: progress by window, offset identity, error components ----------
Dsec = {}
def windows(t):
    edges = [0, 11.96, 19.45, 43.38, 50.87, 62.832]
    names = ['along_0', 'lateral_1', 'along_2', 'lateral_3', 'along_4']
    return [(nm, (t >= a) & (t < b)) for nm, a, b in zip(names, edges[:-1], edges[1:])]
for cn in ['retained-approach', 'combined-mild-approach', 'retained-across_10deg', 'motion-only-mild-across', 'combined-mild-across',
           'retained-along_10deg', 'combined-mild-along', 'retained-strong-frozen', 'combined-strong-approach']:
    D = L(cn); t = D['t']; n = D['n_true']; ne = D['n_est']; g = gated(D); ff = D['f_filt']; e = D['path_err_base']
    Pe = proj(ne, e); Pf = proj(ne, ff); ident = np.linalg.norm(Pe - Pf / KP, axis=1)
    late = t >= 5.0
    d, b = frame(n, D['ref_vel']); e_al, e_ac = components_deg(ne, n, d, b)
    prog = {nm: {'progress_ratio': r(np.sum(D['act_progress'][m]) / np.sum(D['ref_progress'][m])), 'gate_open': r(g[m].mean()),
                 'err_along_deg_mean': r(e_al[m].mean()), 'err_across_deg_mean': r(e_ac[m].mean()), 'n_err_deg_rms': r(np.degrees(rms(D['n_err'][m])))}
            for nm, m in windows(t)}
    Dsec[cn] = {'progress_ratio_total': r(np.sum(D['act_progress']) / np.sum(D['ref_progress'])), 'gate_open_total': r(g.mean()),
                'offset_identity_residual_rms_mm_after_5s': r(1e3 * rms(ident[late])), 'path_offset_est_frame_rms_mm_after_5s': r(1e3 * rms(np.linalg.norm(Pe, axis=1)[late])),
                'tangential_force_est_frame_rms_n_after_5s': r(rms(np.linalg.norm(Pf, axis=1)[late])),
                'tilt_leak_proxy_5sin_err_rms_n_after_5s': r(rms(TARGET * np.sin(D['n_err'][late]))),
                'friction_mu_N_mean_n': r((MU * D['load_true']).mean()), 'load_true_mean_n': r(D['load_true'].mean()),
                'path_rms_mm_true_tangent': r(1e3 * rms(D['path_err'])), 'qp_ticks': int(D['qp'].sum()), 'saturation_ticks': int(D['saturated'].sum()),
                'n_err_deg_at': {str(int(a)): r(np.degrees(D['n_err'][(t >= a) & (t < a + 1)]).mean()) for a in (0, 5, 10, 15, 20, 30, 40, 50, 60)},
                'err_components_deg_at': {str(int(a)): [r(e_al[(t >= a) & (t < a + 1)].mean()), r(e_ac[(t >= a) & (t < a + 1)].mean())] for a in (0, 5, 10, 15, 20, 30, 40, 50, 60)},
                'windows': prog}
out['D_priors'] = Dsec

# ---------- E. entry adaptation ----------
E = {}
for cn in ['combined-mild-approach', 'combined-mild-along', 'combined-mild-across', 'combined-strong-approach', 'combined-normal-hold',
           'combined-tangent-hold', 'motion-only-mild-across', 'frozen-normal-hold', 'frozen-tangent-hold']:
    D = L(cn); S = SurfaceField(**json.loads(str(D['surface'])))
    ep = D['e_pos']; en = D['e_n_est']; ev = D['e_v']; eg = D['e_contact_gate'] & D['e_excitation_gate']
    n_true_e = np.asarray([-S.true_outward_normal(p) for p in ep])
    err_e = ang(en, n_true_e)
    n0 = D['n0']; init_err = ang(n0[None, :], n_true_e[:1])[0]
    Dp = D; first_path_true = Dp['n_true'][0]
    path_start_err = ang(Dp['n_est'][:1], first_path_true[None, :])[0]  # post-first-PATH-update estimate vs truth (diagnostic)
    n_est_at_path_start = en[-1]  # last entry tick estimate == PATH-start estimate (before first PATH update)
    path_start_err_pre = ang(n_est_at_path_start[None, :], first_path_true[None, :])[0]
    an = np.sum(en * ev, -1); pv = np.linalg.norm(proj(en, ev), axis=1)
    E[cn] = {'initial_error_deg': r(init_err), 'path_start_error_deg_pre_update': r(path_start_err_pre), 'entry_gate_open': r(eg.mean()),
             'entry_first_gated_tick': (int(np.where(eg)[0][0]) if eg.any() else None),
             'entry_normal_over_tangent_speed_median_gated': (r(np.median(np.abs(an[eg]) / np.maximum(pv[eg], 1e-9))) if eg.any() else None),
             'entry_energy_ratio_gated': (r(np.sum(an[eg] ** 2) / max(np.sum(pv[eg] ** 2), 1e-18)) if eg.any() else None),
             'entry_cp_residual_rms_gated': r(rms(D['e_cp_residual'][eg])) if eg.any() else None,
             'entry_error_trace_deg_every_100ms': [r(x) for x in err_e[::50]],
             'entry_motion_applied_fraction': r(D['e_motion_applied'].mean()), 'entry_cp_applied_fraction': r(D['e_cp_applied'].mean())}
out['E_entry'] = E
print(json.dumps(out))
