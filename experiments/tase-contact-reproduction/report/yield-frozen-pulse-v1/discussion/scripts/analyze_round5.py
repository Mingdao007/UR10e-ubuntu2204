"""Round-5 read-only post-processing of FP-v1 / FT-v1 / FM-v1 / GM-v1 receipts.

Input: /tmp/yfp5/*.npz produced by extract_ticks_v5.py (see fable-round5.md Reproduction).
Output: JSON on stdout (retained as data/round5-analysis.json).  Nothing is re-run.
All clocks are the PATH clock (rows[*].time_s); release for the pulse is 20.5 s, for the
sustained scenarios 35.5 s.  Evaluator truth (true load, external force, true normal) is
used only to label mechanisms; nothing here is a controller-computable gate.
"""
import json, sys
import numpy as np

CACHE = '/tmp/yfp5'
L = lambda k: np.load(f'{CACHE}/{k}.npz')
PB, FB = 0.002, 0.5
out = {}

def recovery(t, d, dl, pb=PB, fb=FB, rel=20.5):
    post = t >= rel
    good = (d <= pb) & (np.abs(dl) <= fb)
    suf = np.logical_and.accumulate(good[::-1])[::-1]
    idx = np.where(post & suf & (t <= t[-1] - .1))[0]
    return float(t[idx[0]] - rel) if len(idx) else None

def pair_arrays(pk, nk):
    P, N = L(pk), L(nk); t = P['t']; assert np.allclose(t, N['t'])
    d = np.linalg.norm(P['row_pos'] - N['row_pos'], axis=1)
    dl = P['load_true'] - N['load_true']
    return P, N, t, d, dl

def envelope(x, w=250):
    return np.array([x[max(0, i - w):i + 1].max() for i in range(len(x))])

def first_stay_below(tp, env, v):
    for i in range(len(env)):
        if env[i] < v and np.all(env[i:] < v):
            return float(tp[i])
    return None

# ---------------------------------------------------------------- 1. pulse: band binding, sensitivity, envelope
pulse = {'SFC': ('pulse-SFC', 'nom-SFC'), 'DSFC': ('pulse-DSFC', 'nom-DSFC'),
         'MSFC': ('pulse-MSFC', 'nom-MSFC'), 'MSFC-identity': ('pulse-MSFCid', 'nom-MSFCid'),
         'MSFC@1ms': ('fine-pulse-MSFC', 'fine-nom-MSFC'), 'MSFC-identity@1ms': ('fine-pulse-MSFCid', 'fine-nom-MSFCid')}
sec = {}
for name, (pk, nk) in pulse.items():
    P, N, t, d, dl = pair_arrays(pk, nk)
    post = t >= 20.5
    rec = recovery(t, d, dl)
    good = (d <= PB) & (np.abs(dl) <= FB)
    last = np.where(post & ~good)[0][-1]
    tp = t[post] - 20.5; env = envelope(np.abs(dl[post]))
    m = (tp >= 1) & (tp <= 3)
    slope = float(np.polyfit(tp[m], np.log(env[m]), 1)[0])
    win = lambda a, b: (t >= a) & (t < b)
    def dom(x, msk):
        y = x[msk] - x[msk].mean(); f = np.fft.rfftfreq(len(y), float(P['dt'])); s = np.abs(np.fft.rfft(y)); s[0] = 0
        return float(f[s.argmax()]), float(2 * s.max() / len(y))
    active = (t >= 20.0) & (t < 20.5)
    sec[name] = {
        'recovery_s': rec,
        'last_violation': {'t_after_release_s': float(t[last] - 20.5), 'distance_mm': float(d[last] * 1e3),
                           'abs_delta_load_n': float(abs(dl[last])), 'binding_band': 'force' if abs(dl[last]) > FB else 'path'},
        'yield_peak_mm_during_pulse': float(d[active].max() * 1e3),
        'post_release_max_distance_mm': float(d[post].max() * 1e3),
        'post_release_max_distance_at_s': float(t[post][d[post].argmax()] - 20.5),
        'true_load_min_n': float(P['load_true'][t >= 19.5].min()), 'true_load_max_n': float(P['load_true'][t >= 19.5].max()),
        'recovery_vs_force_band_s': {str(fb): recovery(t, d, dl, PB, fb) for fb in (0.3, 0.4, 0.5, 0.6, 0.8, 1.0)},
        'recovery_vs_path_band_s': {str(pb): recovery(t, d, dl, pb, FB) for pb in (0.0015, 0.002, 0.003)},
        'envelope_stay_below_s': {str(v): first_stay_below(tp, env, v) for v in (2, 1, 0.5, 0.25)},
        'envelope_log_slope_1_to_3_s_per_s': slope, 'envelope_tau_s': float(-1 / slope),
        'nominal_load_std_n_15_20': float(N['load_true'][win(15, 20)].std()),
        'pulse_trial_load_std_n': {f'{a}_{b}': float(P['load_true'][win(a, b)].std()) for a, b in ((22, 24), (24, 26), (26, 28), (30, 35))},
        'abs_delta_load_max_n': {f'{a}_{b}': float(np.abs(dl[win(a, b)]).max()) for a, b in ((22, 24), (24, 26), (26, 28), (30, 35))},
        'delta_load_dominant_hz_amp_n': {f'{a}_{b}': dom(dl, win(a, b)) for a, b in ((24, 26), (30, 35))},
        'nominal_load_dominant_hz_amp_n_15_20': dom(N['load_true'], win(15, 20)),
        'saturation_ticks_pulse_trial': int(P['saturated'].sum()), 'qp_ticks': int(P['qp'].sum()),
    }
out['pulse_recovery'] = sec

# ---------------------------------------------------------------- 2. memory timeline in the pulse (on vs identity)
def mem(P):
    l22 = P['law22']; eig = l22[:, 19:22]; h = l22[:, 7:10]; S = l22[:, 10:19].reshape(-1, 3, 3)
    return eig.min(axis=1), np.linalg.norm(h, axis=1), S
P_on, N_on = L('pulse-MSFC'), L('nom-MSFC'); P_id, N_id = L('pulse-MSFCid'), L('nom-MSFCid')
t = P_on['t']
eig_on, h_on, S_on = mem(P_on); eig_id, h_id, _ = mem(P_id); eig_nom, h_nom, _ = mem(N_on)
lf_on, lf_id = P_on['law_force'], P_id['law_force']; lv_on, lv_id = P_on['law_vel'], P_id['law_vel']
dload = P_on['load_true'] - P_id['load_true']
bins = {}
for a in (19.5, 20.0, 20.1, 20.2, 20.3, 20.4, 20.5, 20.75, 21.0, 21.5, 22.0, 23.0, 25.0, 30.0):
    b = a + (0.1 if a < 20.5 else 0.25 if a < 21 else 0.5 if a < 23 else 1.0)
    m = (t >= a) & (t < b)
    bins[f'{a}'] = {'eig_min_on': float(eig_on[m].min()), 'eig_min_identity': float(eig_id[m].min()), 'eig_min_nominal_on': float(eig_nom[m].min()),
                    'h_norm_on_mean_n': float(h_on[m].mean()), 'law_input_norm_on_mean_n': float(np.linalg.norm(lf_on[m], axis=1).mean()),
                    'law_input_diff_on_minus_id_max_n': float(np.linalg.norm(lf_on[m] - lf_id[m], axis=1).max()),
                    'law_output_diff_on_minus_id_max_mm_s': float(np.linalg.norm(lv_on[m] - lv_id[m], axis=1).max() * 1e3),
                    'true_load_on_minus_id_max_abs_n': float(np.abs(dload[m]).max()),
                    'true_load_on_max_n': float(P_on['load_true'][m].max()), 'true_load_id_max_n': float(P_id['load_true'][m].max())}
i_pk_on = int(np.argmax(P_on['load_true'])); i_pk_id = int(np.argmax(P_id['load_true']))
i_eig = int(np.argmin(eig_on))
out['pulse_memory_timeline'] = {
    'bins': bins,
    'peak_load_on': {'t': float(t[i_pk_on]), 'load_n': float(P_on['load_true'][i_pk_on]), 'eig_min_at_peak': float(eig_on[i_pk_on])},
    'peak_load_identity': {'t': float(t[i_pk_id]), 'load_n': float(P_id['load_true'][i_pk_id])},
    'eig_min_overall': {'t': float(t[i_eig]), 'value': float(eig_on[i_eig])},
    'eig_below_0_95_window_s': [float(t[np.where(eig_on < 0.95)[0][0]]), float(t[np.where(eig_on < 0.95)[0][-1]])] if np.any(eig_on < 0.95) else None,
    'eig_below_0_99_fraction_of_cycle': float(np.mean(eig_on < 0.99)),
    'nominal_eig_min_overall': float(eig_nom.min()),
    'identity_eig_exact_one': bool(np.all(eig_id == 1.0)),
    'law_state_dim_check_w_equals_output_over_g': float(np.max(np.abs(P_on['law22'][:, 4:7] * 0.08583341909109758 - P_on['law_vel']))),
}
# tangent hold and normal hold memory timelines
def mem_summary(P, N, windows):
    e, h, _ = mem(P); en, hn, _ = mem(N); t = P['t']; r = {}
    for lab, (a, b) in windows.items():
        m = (t >= a) & (t < b)
        r[lab] = {'eig_min': float(e[m].min()), 'eig_mean': float(e[m].mean()), 'eig_min_nominal': float(en[m].min()), 'h_norm_mean_n': float(h[m].mean()),
                  'law_input_norm_mean_n': float(np.linalg.norm(P['law_force'][m], axis=1).mean())}
    return r
W = {'pre_15_20': (15, 20), 'onset_20_20.5': (20, 20.5), 'hold_20.5_30.5': (20.5, 30.5), 'release_30.5_35.5': (30.5, 35.5), 'post_35.5_40': (35.5, 40)}
out['tangent_hold_memory'] = mem_summary(L('tan-MSFC'), L('nom-MSFC'), W)
out['normal_hold_memory_legacy_gm'] = mem_summary(L('gm-normal-g50on'), L('gm-nom-g50on'), W)

# ---------------------------------------------------------------- 3. tangent hold: band binding and memory pair
tan = {'SFC': ('tan-SFC', 'nom-SFC'), 'DSFC': ('tan-DSFC', 'nom-DSFC'), 'MSFC': ('tan-MSFC', 'nom-MSFC'), 'MSFC-identity': ('tan-MSFCid', 'nom-MSFCid')}
sec = {}
for name, (pk, nk) in tan.items():
    P, N, t, d, dl = pair_arrays(pk, nk); rel = 35.5; post = t >= rel
    rec = recovery(t, d, dl, rel=rel); good = (d <= PB) & (np.abs(dl) <= FB); last = np.where(post & ~good)[0][-1]
    hold = (t >= 20.5) & (t < 30.5)
    sec[name] = {'recovery_s': rec, 'binding_band': 'force' if abs(dl[last]) > FB else 'path',
                 'last_violation_distance_mm': float(d[last] * 1e3), 'last_violation_abs_delta_load_n': float(abs(dl[last])),
                 'recovery_vs_force_band_s': {str(fb): recovery(t, d, dl, PB, fb, rel) for fb in (0.3, 0.5, 1.0)},
                 'recovery_vs_path_band_s': {str(pb): recovery(t, d, dl, pb, FB, rel) for pb in (0.001, 0.002, 0.003)},
                 'post_release_max_abs_delta_load_n': float(np.abs(dl[post]).max()), 'post_release_max_distance_mm': float(d[post].max() * 1e3),
                 'yield_peak_mm': float(d[(t >= 20) & (t < rel)].max() * 1e3), 'hold_mean_distance_mm': float(d[hold].mean() * 1e3),
                 'distance_at_release_mm': float(d[np.where(post)[0][0]] * 1e3),
                 'true_load_min_hold_n': float(P['load_true'][hold].min()), 'true_load_max_post_n': float(P['load_true'][post].max()),
                 'saturation_ticks': int(P['saturated'].sum())}
out['tangent_recovery'] = sec
# on-vs-identity tangent pair: direct trace differences
P1, P2 = L('tan-MSFC'), L('tan-MSFCid'); t = P1['t']
out['tangent_on_minus_identity'] = {lab: {'max_abs_true_load_diff_n': float(np.abs(P1['load_true'][m] - P2['load_true'][m]).max()),
                                          'max_tcp_diff_mm': float(np.linalg.norm(P1['row_pos'][m] - P2['row_pos'][m], axis=1).max() * 1e3),
                                          'max_law_output_diff_mm_s': float(np.linalg.norm(P1['law_vel'][m] - P2['law_vel'][m], axis=1).max() * 1e3)}
                                    for lab, m in (('hold', (t >= 20.5) & (t < 30.5)), ('release', (t >= 30.5) & (t < 35.5)), ('post', t >= 35.5))}

# ---------------------------------------------------------------- 4. identifiability during the pulse (frozen observer)
P = L('pulse-MSFC'); t = P['t']; n = P['n_est']; fext = P['f_ext']; ntrue = P['n_true']
act = (t >= 20.0) & (t < 20.5)
fe_n = np.einsum('ij,ij->i', fext, n); fe_t = np.linalg.norm(fext - fe_n[:, None] * n, axis=1)
ff = P['f_filt']; ff_n = np.einsum('ij,ij->i', ff, n); ff_t = np.linalg.norm(ff - ff_n[:, None] * n, axis=1)
mat = json.loads(str(P['material'])); mu = mat['friction_mu']
i = int(np.argmax(np.linalg.norm(fext, axis=1)))
ext_dir = fext[i] / np.linalg.norm(fext[i])
out['pulse_identifiability'] = {
    'external_force_peak_n': float(np.linalg.norm(fext[i])), 'external_force_peak_t': float(t[i]),
    'external_direction_vs_estimated_normal_deg': float(np.degrees(np.arccos(abs(ext_dir @ n[i])))),
    'external_direction_vs_true_normal_deg': float(np.degrees(np.arccos(abs(ext_dir @ ntrue[i])))),
    'external_normal_component_peak_n': float(np.abs(fe_n[act]).max()), 'external_tangential_component_peak_n': float(fe_t[act].max()),
    'external_normal_sign_at_peak': 'inward (adds to sensed load)' if fe_n[i] > 0 else 'outward (pull)',
    'sensed_signed_load_at_peak_n': float(P['signed_load'][i]), 'true_load_at_peak_n': float(P['load_true'][i]),
    'sensed_minus_true_load_max_abs_n': float(np.abs(P['signed_load'][act] - P['load_true'][act]).max()),
    'tangential_filtered_force_peak_n': float(ff_t[act].max()), 'mu_times_true_load_at_that_tick_n': float(mu * P['load_true'][act][ff_t[act].argmax()]),
    'tangential_filtered_force_nominal_rms_15_20_n': float(np.sqrt(np.mean(ff_t[(t >= 15) & (t < 20)] ** 2))),
    'friction_bound_mu_hat_0.15_times_5N': 0.75, 'filter_tau_s': json.loads(str(P['settings']))['filter_tau_s'],
    'pulse_width_s': 0.5, 'frozen_normal_error_deg_at_peak': float(np.degrees(P['n_err'][i])),
}

# ---------------------------------------------------------------- 5. frozen-observer error and nominal behaviour
sec = {}
for name, nk in (('SFC', 'nom-SFC'), ('DSFC', 'nom-DSFC'), ('MSFC', 'nom-MSFC'), ('MSFC-identity', 'nom-MSFCid')):
    N = L(nk); t = N['t']
    y = N['load_true'][(t >= 15) & (t < 20)]; f = np.fft.rfftfreq(len(y), float(N['dt'])); s = np.abs(np.fft.rfft(y - y.mean())); s[0] = 0
    sec[name] = {'normal_error_rms_deg': float(np.degrees(np.sqrt(np.mean(N['n_err'] ** 2)))), 'normal_error_max_deg': float(np.degrees(N['n_err'].max())),
                 'load_mae_n': float(np.mean(np.abs(N['load_true'] - 5.0))), 'load_std_15_20_n': float(y.std()),
                 'load_dominant_hz_15_20': float(f[s.argmax()]), 'load_dominant_amp_n': float(2 * s.max() / len(y)),
                 'path_rms_mm': float(np.sqrt(np.mean(N['path_err'] ** 2)) * 1e3), 'progress_ratio': float(N['act_progress'].sum() / N['ref_progress'].sum()),
                 'law_input_norm_rms_n': float(np.sqrt(np.mean(np.linalg.norm(N['law_force'], axis=1) ** 2)))}
out['frozen_nominal'] = sec

# ---------------------------------------------------------------- 6. structural: law operating points [S]
laws = {'SFC': dict(m=4.0, mu=393.0, n=3.0, g=0.052126826121414976, a=0.0, p=1.0),
        'DSFC': dict(m=4.0, mu=310.66177089084647, n=3.0, g=0.0642, a=0.05, p=0.5),
        'MSFC g50 (identity metric)': dict(m=4.0, mu=1228.1391393367705, n=3.0, g=0.08583341909109758, a=0.05, p=0.5)}
sec = {}
for name, q in laws.items():
    row = {}
    for U in (0.1, 0.5, 1.0, 3.0):
        # steady state |u| = a r^p + mu r^n  (solve by bisection)
        lo, hi = 1e-9, 10.0
        for _ in range(200):
            r = 0.5 * (lo + hi); val = q['a'] * r ** q['p'] + q['mu'] * r ** q['n']
            lo, hi = (r, hi) if val < U else (lo, r)
        r = 0.5 * (lo + hi)
        dinc = q['a'] * q['p'] * r ** (q['p'] - 1) + q['n'] * q['mu'] * r ** (q['n'] - 1)  # incremental damping dD/dr
        row[f'|u|={U}N'] = {'steady_output_mm_s': float(q['g'] * r * 1e3), 'incremental_damping_N_s': float(dinc),
                            'time_constant_m_over_dinc_s': float(q['m'] / dinc), 'mobility_g_over_dinc_mm_s_per_N': float(q['g'] / dinc * 1e3)}
    sec[name] = row
out['law_operating_points'] = sec
out['law_operating_points_note'] = ('Continuous-form algebra with the receipts\' parameters; a=0 for SFC. Per-axis SFC and radial DSFC/MSFC coincide '
                                    'on a one-axis restriction. Not a closed-loop stability statement; the closed loop adds filter (20 ms), servo, contact and the 120 N/m path spring.')

# ---------------------------------------------------------------- 7. robust load-excursion descriptors (threshold-free) and nominal bias split
sec = {}
for name, (pk, nk) in pulse.items():
    P, N, t, d, dl = pair_arrays(pk, nk); dt = float(P['dt']); ev = (t >= 20.0) & (t < 23.0); post = t >= 20.5
    load = P['load_true']
    sec[name] = {'below_2.5N_s': float(np.sum(load[ev] < 2.5) * dt), 'below_4N_s': float(np.sum(load[ev] < 4.0) * dt),
                 'above_6N_s': float(np.sum(load[ev] > 6.0) * dt), 'above_7N_s': float(np.sum(load[ev] > 7.0) * dt),
                 'integral_abs_load_error_20_23_N_s': float(np.sum(np.abs(load[ev] - 5.0)) * dt),
                 'integral_abs_load_error_23_35_N_s': float(np.sum(np.abs(load[(t >= 23) & (t < 35)] - 5.0)) * dt),
                 'integral_abs_delta_load_post_N_s': float(np.sum(np.abs(dl[post])) * dt),
                 'integral_distance_post_mm_s': float(np.sum(d[post]) * dt * 1e3),
                 'rebound_peak_minus_nominal_peak_n': float(load.max() - N['load_true'].max())}
out['pulse_robust_load'] = sec
sec = {}
for name, nk in (('SFC', 'nom-SFC'), ('DSFC', 'nom-DSFC'), ('MSFC', 'nom-MSFC'), ('MSFC-identity', 'nom-MSFCid')):
    N = L(nk); t = N['t']; e = N['load_true'] - 5.0
    sec[name] = {f'{a}_{b}': {'mean_n': float(e[(t >= a) & (t < b)].mean()), 'std_n': float(e[(t >= a) & (t < b)].std())}
                 for a, b in ((0, 5), (5, 20), (20, 40), (40, 62.8))}
    sec[name]['entry_first_0.5s_max_abs_n'] = float(np.abs(e[t < 0.5]).max())
out['frozen_nominal_bias'] = sec

# ---------------------------------------------------------------- 8. nominal load error by window (entry-transient decay versus steady following)
sec = {}
for name, nk in (('SFC', 'nom-SFC'), ('DSFC', 'nom-DSFC'), ('MSFC', 'nom-MSFC'), ('MSFC-identity', 'nom-MSFCid')):
    N = L(nk); t = N['t']; e = N['load_true'] - 5.0
    sec[name] = {f'{a}_{b}': {'mae_n': float(np.abs(e[(t >= a) & (t < b)]).mean()), 'std_n': float(e[(t >= a) & (t < b)].std()),
                              'max_abs_n': float(np.abs(e[(t >= a) & (t < b)]).max())}
                 for a, b in ((0, 2), (2, 5), (5, 10), (10, 15), (15, 20), (20, 62.9))}
out['frozen_nominal_windows'] = sec
json.dump(out, sys.stdout, indent=1)
