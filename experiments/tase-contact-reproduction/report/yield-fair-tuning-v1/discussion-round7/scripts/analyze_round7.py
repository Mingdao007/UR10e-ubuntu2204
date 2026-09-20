"""Round-7 read-only post-processing.  Nothing is re-run; no native law is stepped.

Inputs: /tmp/yfp5 and /tmp/yfp6 tick caches (round-5 extract_ticks_v5.py, round-6 extract_rows_v6.py),
re-hashed here against the receipts on disk; the retained warm-memory artifact
runs/yield-warm-memory-v1/native-history.json.gz; the validation reservation protocol; and
data/round6-analysis.json for cross-checks.  Output: JSON on stdout (retained as data/round7-analysis.json).
PATH clock throughout.  Slot layout of law22: 4-6 velocity state w, 7-9 history h, 10-18 structure S,
19-21 metric eigenvalues.
"""
import json, hashlib, gzip, math, itertools
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[4]
C5, C6 = '/tmp/yfp5', '/tmp/yfp6'
L5 = lambda k: np.load(f'{C5}/{k}.npz'); L6 = lambda k: np.load(f'{C6}/{k}.npz')
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
FB, XB, TB = 0.5, 0.002, 0.05
ONSET, PULSE_REL, SUST_REL, HOLD_END = 20.0, 20.5, 35.5, 30.5
out = {'scope': 'round-7 advisory post-processing of retained receipts; development data; no run, no formal unit'}
R6 = json.loads((ROOT / 'report/yield-fair-tuning-v1/discussion/data/round6-analysis.json').read_text())

# ---------------------------------------------------------------- 0. cache integrity against the receipts on disk
used5 = {'nom-SFC': 'runs/yield-frozen-transfer-v1/SFC-nominal.json.gz', 'nom-DSFC': 'runs/yield-observer-prior-v1/DSFC-stiff_low_mu-approach.json.gz',
         'nom-MSFC': 'runs/yield-frozen-transfer-v1/MSFC-nominal.json.gz', 'nom-MSFCid': 'runs/yield-frozen-memory-v1/MSFC-nominal.json.gz',
         'tan-SFC': 'runs/yield-frozen-transfer-v1/SFC-sustained_release_tangent.json.gz', 'tan-DSFC': 'runs/yield-normal-v3/frozen-tangent-hold.json.gz',
         'tan-MSFC': 'runs/yield-frozen-transfer-v1/MSFC-sustained_release_tangent.json.gz', 'tan-MSFCid': 'runs/yield-frozen-memory-v1/MSFC-sustained_release_tangent.json.gz',
         'pulse-SFC': 'runs/yield-frozen-pulse-v1/SFC/SFC-short_pulse_oblique.json.gz', 'pulse-DSFC': 'runs/yield-frozen-pulse-v1/DSFC/DSFC-short_pulse_oblique.json.gz',
         'pulse-MSFC': 'runs/yield-frozen-pulse-v1/MSFC/MSFC-short_pulse_oblique.json.gz', 'pulse-MSFCid': 'runs/yield-frozen-pulse-v1/MSFC-identity/MSFC-short_pulse_oblique.json.gz'}
used6 = {'no3-nom-SFC': 'runs/yield-observer-transfer-v1/SFC-nominal.json.gz', 'no3-nrm-SFC': 'runs/yield-observer-transfer-v1/SFC-sustained_release_normal.json.gz',
         'no3-tan-SFC': 'runs/yield-observer-transfer-v1/SFC-sustained_release_tangent.json.gz', 'no3-nom-DSFC': 'runs/yield-normal-v3/combined-mild-approach.json.gz',
         'no3-nrm-DSFC': 'runs/yield-normal-v3/combined-normal-hold.json.gz', 'no3-tan-DSFC': 'runs/yield-normal-v3/combined-tangent-hold.json.gz',
         'no3-nom-MSFC': 'runs/yield-observer-transfer-v1/MSFC-nominal.json.gz', 'no3-nrm-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_normal.json.gz',
         'no3-tan-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_tangent.json.gz', 'no3-strong-DSFC': 'runs/yield-normal-v3/combined-strong-approach.json.gz'}
integ = []
for cache, table, L in ((C5, used5, L5), (C6, used6, L6)):
    for k, rel in table.items():
        integ.append({'cache_key': k, 'cache': cache, 'path': rel, 'sha256_file_now': sha(ROOT / rel), 'sha256_in_cache': str(L(k)['sha256'])})
        integ[-1]['match'] = integ[-1]['sha256_file_now'] == integ[-1]['sha256_in_cache']
out['cache_integrity'] = {'receipts': integ, 'all_match': all(e['match'] for e in integ), 'count': len(integ)}
assert out['cache_integrity']['all_match'], 'cache/receipt hash mismatch'

# ---------------------------------------------------------------- 1. Q4: relative-to-nominal attitude term versus absolute attitude (NO-v3 holds)
def j_terms(P, N, rel):
    t = P['t']; dt = float(P['dt']); assert np.allclose(t, N['t'])
    dl = np.abs(P['load_true'] - N['load_true']); dx = np.linalg.norm(P['row_pos'] - N['row_pos'], axis=1)
    da = np.abs(P['orient_err'] - N['orient_err'])
    ml = t >= ONSET; mp = t >= rel
    jl = float(dl[ml].sum() * dt / FB); jp = float(dx[mp].sum() * dt / XB); ja = float(da[ml].sum() * dt / TB)
    return {'J_load_s': jl, 'J_path_s': jp, 'J_att_s': ja, 'J3_s': jl + jp + ja,
            'J_att_absolute_post_release_s': float(np.abs(P['orient_err'][mp]).sum() * dt / TB),
            'J_att_absolute_onset_to_end_s': float(np.abs(P['orient_err'][ml]).sum() * dt / TB),
            'nominal_att_absolute_onset_to_end_s': float(np.abs(N['orient_err'][ml]).sum() * dt / TB),
            'disturbed_post_release_att_rms_deg': float(np.degrees(np.sqrt(np.mean(P['orient_err'][mp] ** 2)))),
            'nominal_post_release_att_rms_deg': float(np.degrees(np.sqrt(np.mean(N['orient_err'][mp] ** 2)))),
            'nominal_whole_path_att_rms_deg': float(np.degrees(np.sqrt(np.mean(N['orient_err'] ** 2)))),
            'disturbed_whole_path_att_rms_deg': float(np.degrees(np.sqrt(np.mean(P['orient_err'] ** 2)))),
            'disturbed_max_att_deg': float(np.degrees(np.abs(P['orient_err'][ml]).max())),
            'true_load_max_n': float(P['load_true'][t >= ONSET - .5].max()), 'true_load_min_n': float(P['load_true'][t >= ONSET - .5].min()),
            'disturbed_progress_ratio': float(P['act_progress'].sum() / P['ref_progress'].sum()),
            'post_release_terminal_distance_mm': float(dx[-1] * 1e3), 'post_release_terminal_delta_load_n': float((P['load_true'] - N['load_true'])[-1]),
            'receipts': {'disturbed': str(P['sha256']), 'nominal': str(N['sha256'])}}
q4 = {}
for scen, arms, rel in (('NO-v3 tangent hold', {'SFC': ('no3-tan-SFC', 'no3-nom-SFC'), 'DSFC': ('no3-tan-DSFC', 'no3-nom-DSFC'), 'MSFC': ('no3-tan-MSFC', 'no3-nom-MSFC')}, SUST_REL),
                        ('NO-v3 normal hold', {'SFC': ('no3-nrm-SFC', 'no3-nom-SFC'), 'DSFC': ('no3-nrm-DSFC', 'no3-nom-DSFC'), 'MSFC': ('no3-nrm-MSFC', 'no3-nom-MSFC')}, SUST_REL),
                        ('frozen pulse 2 ms', {'SFC': ('pulse-SFC', 'nom-SFC'), 'DSFC': ('pulse-DSFC', 'nom-DSFC'), 'MSFC': ('pulse-MSFC', 'nom-MSFC'), 'MSFC-identity': ('pulse-MSFCid', 'nom-MSFCid')}, PULSE_REL)):
    Lf = L6 if scen.startswith('NO-v3') else L5
    q4[scen] = {a: j_terms(Lf(p), Lf(n), rel) for a, (p, n) in arms.items()}
    for a in arms:  # cross-check against round 6 values (same definitions, same caches)
        r6 = R6['objective_three_term']['values'][scen][a]
        q4[scen][a]['round6_J3_s'] = r6['J3_s']; q4[scen][a]['abs_diff_vs_round6_J3'] = abs(r6['J3_s'] - q4[scen][a]['J3_s'])
out['q4_relative_vs_absolute'] = q4
tan = q4['NO-v3 tangent hold']
out['q4_counterexample_attitude'] = {
    'statement': 'relative J_att ranks SFC ahead of DSFC in the NO-v3 tangent hold while absolute post-release attitude is the same and the SFC nominal is 2.2x worse',
    'J_att_relative_s': {a: tan[a]['J_att_s'] for a in tan},
    'J_att_absolute_onset_to_end_s': {a: tan[a]['J_att_absolute_onset_to_end_s'] for a in tan},
    'nominal_att_absolute_onset_to_end_s': {a: tan[a]['nominal_att_absolute_onset_to_end_s'] for a in tan},
    'disturbed_post_release_att_rms_deg': {a: tan[a]['disturbed_post_release_att_rms_deg'] for a in tan},
    'nominal_whole_path_att_rms_deg': {a: tan[a]['nominal_whole_path_att_rms_deg'] for a in tan},
    'ordering_relative_J_att': sorted(tan, key=lambda a: tan[a]['J_att_s']),
    'ordering_absolute_J_att': sorted(tan, key=lambda a: tan[a]['J_att_absolute_onset_to_end_s']),
    'credit_SFC_receives_from_worse_nominal_band_s': tan['SFC']['J_att_absolute_onset_to_end_s'] - tan['SFC']['J_att_s'],
    'credit_DSFC_receives_band_s': tan['DSFC']['J_att_absolute_onset_to_end_s'] - tan['DSFC']['J_att_s']}
pulse = q4['frozen pulse 2 ms']
out['q4_counterexample_peak'] = {
    'statement': 'J ranks DSFC ahead of SFC on the frozen oblique pulse while DSFC has the higher contact peak; the peak excess costs almost nothing in J_load',
    'J3_s': {a: pulse[a]['J3_s'] for a in pulse}, 'true_load_max_n': {a: pulse[a]['true_load_max_n'] for a in pulse},
    'peak_cost_arithmetic': {'rule': 'a peak excess dP above the nominal for a duration tau costs dP*tau/0.5 N band-seconds',
        'examples': [{'dP_n': dP, 'tau_s': tau, 'cost_band_s': dP * tau / FB} for dP, tau in ((0.2, 0.1), (1.0, 0.1), (2.9, 0.05), (2.9, 0.2))],
        'resolved_J_difference_on_pulse_pair_band_s': R6['paired_resolution_criterion_demo_pulse']['J3_s']['min_abs_diff']}}
# sustained-offset arithmetic: a permanent post-release offset versus a transient excursion
post_win = 62.83185307179586 - SUST_REL
out['q4_offset_arithmetic'] = {'post_release_window_s': post_win,
    'permanent_offset_cost_band_s': {f'{mm} mm': mm * 1e-3 / XB * post_win for mm in (0.5, 1.0, 1.9)},
    'transient_excursion_cost_band_s': {f'{mm} mm for {s} s': mm * 1e-3 / XB * s for mm, s in ((5.0, 2.0), (8.0, 3.0))},
    'recovery_metric_band_mm': XB * 1e3, 'note': 'a permanent offset under 2 mm counts as recovered by the 2 mm/0.5 N metric and is cheap in J if small; a large transient is expensive in J but recovers'}

# ---------------------------------------------------------------- 2. Q3: closed-loop memory state at onset, hold and release (frozen-transfer MSFC receipts, law22 present)
def mem(A):
    L = A['law22']; w = L[:, 4:7]; h = L[:, 7:10]; S = L[:, 10:19].reshape(-1, 3, 3); lam = L[:, 19:22]
    return {'t': A['t'], 'w': np.linalg.norm(w, axis=1), 'h': np.linalg.norm(h, axis=1), 'S': np.linalg.norm(S.reshape(-1, 9), axis=1),
            'lam_min': lam.min(axis=1), 'lam': lam, 'lf': np.linalg.norm(A['law_force'], axis=1), 'raw': L}
def at(m, t0):
    i = int(np.argmin(np.abs(m['t'] - t0)))
    return {'t_s': float(m['t'][i]), 'w_norm_m_s': float(m['w'][i]), 'h_norm': float(m['h'][i]), 'S_fro': float(m['S'][i]),
            'metric_eigenvalues': [float(x) for x in m['lam'][i]], 'law_force_norm_n': float(m['lf'][i])}
times = [1.0, 5.0, 19.998, 20.5, 25.0, 30.0, 30.5, 33.0, 35.5, 36.0, 36.5, 37.0, 40.0, 50.0, 62.83]
q3 = {}
for key in ('nom-MSFC', 'tan-MSFC', 'pulse-MSFC', 'nom-MSFCid', 'tan-MSFCid', 'pulse-MSFCid'):
    A = L5(key); m = mem(A); prm = json.loads(str(A['parameters']))
    q3[key] = {'receipt': str(A['sha256']), 'scenario': str(A['scenario']), 'minimum_metric_eigenvalue_parameter': prm['minimum_metric_eigenvalue'],
               'samples': [at(m, t0) for t0 in times],
               'trajectory_min_metric_eigenvalue': float(m['lam_min'].min()), 'time_of_min_s': float(m['t'][int(np.argmin(m['lam_min']))]),
               'max_h_norm': float(m['h'].max()), 'max_S_fro': float(m['S'].max()),
               'pre_onset_max_S_fro': float(m['S'][m['t'] < ONSET].max()), 'pre_onset_min_metric_eigenvalue': float(m['lam_min'][m['t'] < ONSET].min())}
# identical pre-onset state between disturbed and nominal (deterministic runner): law22 equality before onset
for d, n in (('tan-MSFC', 'nom-MSFC'), ('pulse-MSFC', 'nom-MSFC')):
    Ld, Ln = L5(d)['law22'], L5(n)['law22']; t = L5(d)['t']; pre = t < ONSET
    q3[d]['pre_onset_law22_max_abs_diff_vs_nominal'] = float(np.abs(Ld[pre] - Ln[pre]).max())
    q3[d]['pre_onset_law22_identical_to_nominal'] = bool(np.array_equal(Ld[pre], Ln[pre]))
# how long after release until the metric returns to within 1 percent of the nominal metric at the same tick
for d, n in (('tan-MSFC', 'nom-MSFC'), ('pulse-MSFC', 'nom-MSFC')):
    md, mn = mem(L5(d)), mem(L5(n)); t = md['t']; rel = SUST_REL if d.startswith('tan') else PULSE_REL
    gap = np.abs(md['lam_min'] - mn['lam_min']); post = t >= rel
    idx = np.where(post & (gap <= 0.01))[0]
    # first time after release from which the gap stays below 1 percent
    suf = np.logical_and.accumulate((gap <= 0.01)[::-1])[::-1]
    idx2 = np.where(post & suf)[0]
    q3[d]['metric_gap_vs_nominal_at_release'] = float(gap[np.argmin(np.abs(t - rel))])
    q3[d]['time_after_release_until_metric_within_1pct_of_nominal_s'] = float(t[idx2[0]] - rel) if len(idx2) else None
    gh = np.abs(md['h'] - mn['h']); suf = np.logical_and.accumulate((gh <= 0.01 * max(md['h'].max(), 1e-12))[::-1])[::-1]; idx3 = np.where(post & suf)[0]
    q3[d]['time_after_release_until_h_within_1pct_of_peak_s'] = float(t[idx3[0]] - rel) if len(idx3) else None
    # w (velocity state) gap versus nominal after release
    gw = np.abs(md['w'] - mn['w']); suf = np.logical_and.accumulate((gw <= 0.01 * max(md['w'].max(), 1e-12))[::-1])[::-1]; idx4 = np.where(post & suf)[0]
    q3[d]['time_after_release_until_w_within_1pct_of_peak_s'] = float(t[idx4[0]] - rel) if len(idx4) else None
    q3[d]['w_peak_m_s'] = float(md['w'].max()); q3[d]['w_nominal_at_release_m_s'] = float(mn['w'][np.argmin(np.abs(t - rel))])
# steady-state formula check [S]: lambda_along_h = lmin + (1-lmin) exp(-kappa*tau_r*|h|^2) at the hold plateau
prm = json.loads(str(L5('tan-MSFC')['parameters'])); mt = mem(L5('tan-MSFC')); i = int(np.argmin(np.abs(mt['t'] - 30.0)))
lmin, kap, taur = prm['minimum_metric_eigenvalue'], prm['kappa_per_n2_s'], prm['tau_recovery_s']
pred = lmin + (1 - lmin) * math.exp(-kap * taur * mt['h'][i] ** 2)
q3['steady_state_formula_check_at_30s_tangent_hold'] = {'h_norm': float(mt['h'][i]), 'predicted_lambda_along_h': pred, 'observed_metric_eigenvalues': [float(x) for x in mt['lam'][i]],
    'formula': 'S_ss = -kappa*tau_r*h h^T (rank one); lambda_i = lmin + (1-lmin)*exp(s_i); s along h = -kappa*tau_r*|h|^2, zero elsewhere', 'abs_error': float(abs(pred - mt['lam'][i].min()))}
# e-folds between the warm baseline and the onset [S]
q3['warm_startup_to_onset_e_folds'] = {'seconds_from_baseline_end_to_onset': 21.0, 'tau_force_s': prm['tau_force_s'], 'tau_recovery_s': prm['tau_recovery_s'],
    'e_folds_h': 21.0 / prm['tau_force_s'], 'e_folds_S': 21.0 / prm['tau_recovery_s'],
    'reading': 'any preparation-specific h or S is replaced by the nominal contact state long before 20 s; the memory state at onset is the nominal-contact state, bitwise identical between the disturbed and the nominal member (see pre_onset_law22_identical_to_nominal)'}
out['q3_closed_loop_memory_state'] = q3

# ---------------------------------------------------------------- 3. Q3: warm-memory artifact (prescribed-input, law only): w versus h/S decay and what carries the command difference
art = ROOT / 'runs/yield-warm-memory-v1/native-history.json.gz'; res = json.loads((ROOT / 'report/yield-warm-memory-v1/result.json').read_text())
wm = {'artifact_sha256_now': sha(art), 'artifact_sha256_in_result': res['artifact']['sha256'], 'check_py_sha256_now': sha(ROOT / 'report/yield-warm-memory-v1/check.py'), 'script_sha256_in_result': res['script_sha256']}
wm['artifact_matches'] = wm['artifact_sha256_now'] == wm['artifact_sha256_in_result']; wm['script_matches'] = wm['check_py_sha256_now'] == wm['script_sha256_in_result']
D = json.loads(gzip.decompress(art.read_bytes()))
rows = {(r['history'], r['zero_input_gap_s']): r for r in D['rows']}
init = {}
for (hist, gap), r in sorted(rows.items()):
    s = np.asarray(r['initial_snapshot']); init[f'{hist}/gap{gap}'] = {'w_norm': float(np.linalg.norm(s[4:7])), 'h_norm': float(np.linalg.norm(s[7:10])), 'S_fro': float(np.linalg.norm(s[10:19])), 'metric_eigenvalues': [float(x) for x in s[19:22]]}
wm['initial_state_by_history_and_gap'] = init
def eff_tau(v0, v1, dtg):
    return None if v0 <= 0 or v1 <= 0 or v1 >= v0 else dtg / math.log(v0 / v1)
gaps = [0.0, 0.2, 0.6, 2.0]
wm['effective_decay_time_s_held_x'] = {'between_gaps': [{'from_s': a, 'to_s': b, 'tau_w': eff_tau(init[f'held_x/gap{a}']['w_norm'], init[f'held_x/gap{b}']['w_norm'], b - a),
    'tau_h': eff_tau(init[f'held_x/gap{a}']['h_norm'], init[f'held_x/gap{b}']['h_norm'], b - a), 'tau_S': eff_tau(init[f'held_x/gap{a}']['S_fro'], init[f'held_x/gap{b}']['S_fro'], b - a)} for a, b in zip(gaps[:-1], gaps[1:])],
    'reading': 'h and S decay with the 0.19/0.20 s time constants; the velocity state w decays with a growing effective time (nonlinear n=3 damping) and is the state that survives a 2 s gap'}
# command difference held_x gap 2 versus cold gap 2 over the probe, and the memory/velocity state gap at the probe start
cx, cc = rows[('held_x', 2.0)], rows[('cold', 2.0)]
dc = np.abs(np.asarray(cx['commands_m_s']) - np.asarray(cc['commands_m_s'])).max(axis=1)
wm['held_x_gap2_vs_cold_gap2'] = {'max_command_difference_m_s': float(dc.max()), 'tick_of_max': int(np.argmax(dc)), 'difference_at_tick_0_m_s': float(dc[0]), 'difference_at_tick_499_m_s': float(dc[-1]),
    'reported_in_result_json': res['summary'][3]['warm_x_vs_cold_max_command_difference_m_s'],
    'initial_w_norm_held_x': init['held_x/gap2.0']['w_norm'], 'initial_w_norm_cold': init['cold/gap2.0']['w_norm'], 'initial_S_fro_held_x': init['held_x/gap2.0']['S_fro'], 'initial_h_norm_held_x': init['held_x/gap2.0']['h_norm'],
    'g_times_initial_w_gap_m_s': D['parameters']['g'] * (init['held_x/gap2.0']['w_norm'] - init['cold/gap2.0']['w_norm']),
    'reading': 'g*w(0) alone equals the command difference at tick 0 (output is g*w); h and S are 1e-4 of their held values, so the retained difference at 2 s is the velocity state, as the README says'}
out['q3_warm_memory_artifact'] = wm

# ---------------------------------------------------------------- 4. Q2: reserved-cell design matrix, aliasing, nearest evidence, cost
prot = json.loads((ROOT / 'report/yield-validation-reservation-v1/protocol.json').read_text())
cells = prot['cells']; F = ['material', 'surface', 'prior_direction', 'prior_angle_deg', 'preparation', 'disturbed_scenario']
val = lambda c, f: (f"({c['surface_parameters']['kappa_xx']},{c['surface_parameters']['kappa_yy']})" if f == 'surface' else str(c[f]))
matrix = [{**{'id': c['id']}, **{f: val(c, f) for f in F}} for c in cells]
levels = {f: sorted(set(val(c, f) for c in cells)) for f in F}
counts = {f: {lv: sum(val(c, f) == lv for c in cells) for lv in levels[f]} for f in F}
alias = {}
for f1, f2 in itertools.combinations(F, 2):
    table = {}
    for c in cells: table.setdefault(val(c, f1), {}).setdefault(val(c, f2), 0); table[val(c, f1)][val(c, f2)] += 1
    # a factor pair is 'separable' only if every level of f1 co-occurs with at least two levels of f2 and vice versa
    sep1 = all(len(v) >= 2 for v in table.values()); inv = {}
    for a, d in table.items():
        for b in d: inv.setdefault(b, set()).add(a)
    sep2 = all(len(v) >= 2 for v in inv.values())
    alias[f'{f1} x {f2}'] = {'table': table, 'both_directions_have_two_levels': sep1 and sep2}
# within-cell arm comparison is unaffected by aliasing; record what each cell can and cannot attribute
q2 = {'design_matrix': matrix, 'level_counts': counts, 'pairwise_aliasing': alias,
      'scenario_appears_once_per_cell': all(v == 1 for v in counts['disturbed_scenario'].values()),
      'warm_only_with_pulses': sorted(c['disturbed_scenario'] for c in cells if c['preparation'] == 'warm'),
      'surface_3_4_cells': [c['id'] for c in cells if c['surface_parameters']['kappa_xx'] == 3.0],
      'nearest_existing_evidence_for_3_4_surface': {'DSFC seed nominal at (6,8), NO-v3': R6['nominal_recomputed']['DSFC/NO-v3/strong_surface'] | {},
                                                  'DSFC seed nominal at (0.8,0.4), NO-v3': {k: R6['nominal_recomputed']['DSFC/NO-v3'][k] for k in ('path_rmse_mm', 'progress_ratio', 'orientation_rmse_deg', 'force_mae_n', 'force_peak_n', 'receipt_sha256')}},
      'training_bands_from_round6': {'path_rmse_mm_max': 5.48, 'progress_min': 0.85, 'attitude_rms_deg_max': 2.81, 'force_mae_n_max': 0.127, 'peak_n_max': 6.27},
      'seed_pulse_peaks_vs_8N_guard': {a: pulse[a]['true_load_max_n'] for a in pulse}}
for k in ('estimator_parameters', 'law_parameters'):
    q2['nearest_existing_evidence_for_3_4_surface']['DSFC seed nominal at (6,8), NO-v3'].pop(k, None)
# curvature-rate arithmetic [S]: figure-eight 80x20 mm at omega 0.1 rad/s
a_m, b_m, w = 0.04, 0.01, 0.1
q2['normal_rotation_rate_bound_rad_s'] = {surf: {'kappa_xx*vx_max': kx * a_m * w, 'kappa_yy*vy_max': ky * b_m * 2 * w, 'observer_rate_cap': 0.05}
                                          for surf, (kx, ky) in (('(0.8,0.4)', (0.8, 0.4)), ('(1.2,0.7)', (1.2, 0.7)), ('(3,4)', (3.0, 4.0)), ('(6,8)', (6.0, 8.0)))}
q2['normal_rotation_rate_note'] = 'upper bounds assuming x = 40 mm sin(wt), y = 10 mm sin(2wt); all below the 0.05 rad/s cap, so the (6,8) degradation (progress 0.745) is not a rate-cap effect; the mechanism at (3,4) is untested'
cost = R6['cost_from_manifests']
q2['cost_upper_bound'] = {'trials_by_setting': {'base 2ms/8': 60, 'controller 1ms/4': 60, 'plant 2ms/16': 60},
    'worker_s_per_trial': {'2ms': cost['assumed_worker_s_per_trial_2ms'], '1ms': cost['assumed_worker_s_per_trial_1ms'], '2ms/16 substeps': 'not retained; assumed <= 1 ms cost (round 6)'},
    'worker_hours': {'base': 60 * cost['assumed_worker_s_per_trial_2ms'] / 3600, 'controller_refinement': 60 * cost['assumed_worker_s_per_trial_1ms'] / 3600, 'plant_refinement_upper': 60 * cost['assumed_worker_s_per_trial_1ms'] / 3600}}
q2['cost_upper_bound']['total_worker_hours_upper'] = sum(q2['cost_upper_bound']['worker_hours'].values())
q2['cost_if_refinements_skipped_for_failed_base_pairs'] = 'each failed base pair saves 4 refinement trials; no change to values or grid'
# J scale by scenario at seeds (why pooling J across cells is not admissible)
q2['J3_scale_by_scenario_at_seeds'] = {s: {a: R6['objective_three_term']['values'][s][a]['J3_s'] for a in R6['objective_three_term']['values'][s]} for s in R6['objective_three_term']['values']}
out['q2_reservation_audit'] = q2

# ---------------------------------------------------------------- 5. Q1: how much of the nominal floor moves with the law versus with the observer (round-6 recomputed nominals, cross-checked)
env = R6['common_module_tradeoff']
out['q1_floor_attribution'] = {
    'same_observer_law_swap_path_rmse_mm': env['NO-v3_seed_envelope']['path_rmse_mm_max']['per_method'],
    'same_law_observer_swap_path_ratio': env['ratio_NO-v3_over_legacy']['path_rmse'],
    'law_swap_relative_range_pct': 100 * (max(env['NO-v3_seed_envelope']['path_rmse_mm_max']['per_method'].values()) / min(env['NO-v3_seed_envelope']['path_rmse_mm_max']['per_method'].values()) - 1),
    'observer_swap_ratio_min': min(env['ratio_NO-v3_over_legacy']['path_rmse'].values()),
    'attitude_same_observer_law_swap_deg': env['NO-v3_seed_envelope']['orientation_rmse_deg_max']['per_method'],
    'J3_law_dependent_spread_at_seeds_band_s': {s: max(v['J3_s'] for v in R6['objective_three_term']['values'][s].values()) - min(v['J3_s'] for v in R6['objective_three_term']['values'][s].values()) for s in R6['objective_three_term']['values']},
    'hold_load_floor_band_s_normal_hold': {a: R6['objective_on_existing_pairs']['NO-v3 normal hold'][a]['load_hold_component_s'] for a in ('SFC', 'DSFC', 'MSFC')},
    'known_unresolved_step_case': 'OT-v1/DC-v1 SFC NO-v3 normal hold 1.005 N pointwise between 2 ms/0.25 ms and 1 ms/0.125 ms (round 6 band-rule disclosure, [R])',
    'source': 'data/round6-analysis.json (recomputed from receipts in round 6; hashes re-verified above)'}
# ---------------------------------------------------------------- 6. Q3 under NO-v3: same memory-state analysis on the three NO-v3 MSFC receipts (extracted this round into /tmp/yfp7)
C7 = '/tmp/yfp7'; L7 = lambda k: np.load(f'{C7}/{k}.npz')
used7 = {'no3-nom-MSFC': 'runs/yield-observer-transfer-v1/MSFC-nominal.json.gz', 'no3-tan-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_tangent.json.gz', 'no3-nrm-MSFC': 'runs/yield-observer-transfer-v1/MSFC-sustained_release_normal.json.gz'}
integ7 = []
for k, rel in used7.items():
    e = {'cache_key': k, 'cache': C7, 'path': rel, 'sha256_file_now': sha(ROOT / rel), 'sha256_in_cache': str(L7(k)['sha256'])}; e['match'] = e['sha256_file_now'] == e['sha256_in_cache']; integ7.append(e)
out['cache_integrity']['receipts'] += integ7; out['cache_integrity']['count'] += len(integ7); out['cache_integrity']['all_match'] = all(e['match'] for e in out['cache_integrity']['receipts'])
assert out['cache_integrity']['all_match']
def engagement(m, thr):
    below = m['lam_min'] < thr; t = m['t']; dt = float(t[1] - t[0])
    if not below.any(): return {'threshold': thr, 'duration_s': 0.0, 'intervals_s': []}
    edges = np.diff(np.concatenate(([0], below.astype(int), [0]))); starts = np.where(edges == 1)[0]; ends = np.where(edges == -1)[0]
    return {'threshold': thr, 'duration_s': float(below.sum() * dt), 'intervals_s': [[float(t[a]), float(t[b - 1])] for a, b in zip(starts, ends)][:6]}
q3n = {}
for key, Lf in (('no3-nom-MSFC', L7), ('no3-tan-MSFC', L7), ('no3-nrm-MSFC', L7)):
    A = Lf(key); m = mem(A); prm = json.loads(str(A['parameters'])); est = json.loads(str(A['estimator_parameters']))
    q3n[key] = {'receipt': str(A['sha256']), 'scenario': str(A['scenario']), 'observer': 'NO-v3' if est.get('motion_rate_cap_rad_s') == 0.05 and est.get('coplanarity_gain_s_inv') == 0.3 else 'other',
                'minimum_metric_eigenvalue_parameter': prm['minimum_metric_eigenvalue'], 'samples': [at(m, t0) for t0 in times],
                'trajectory_min_metric_eigenvalue': float(m['lam_min'].min()), 'time_of_min_s': float(m['t'][int(np.argmin(m['lam_min']))]),
                'max_h_norm': float(m['h'].max()), 'max_S_fro': float(m['S'].max()), 'pre_onset_min_metric_eigenvalue': float(m['lam_min'][m['t'] < ONSET].min()),
                'engagement_windows': [engagement(m, thr) for thr in (0.99, 0.95, 0.90)]}
for d in ('no3-tan-MSFC', 'no3-nrm-MSFC'):
    Ld, Ln = L7(d)['law22'], L7('no3-nom-MSFC')['law22']; t = L7(d)['t']; pre = t < ONSET
    q3n[d]['pre_onset_law22_identical_to_nominal'] = bool(np.array_equal(Ld[pre], Ln[pre]))
    md, mn = mem(L7(d)), mem(L7('no3-nom-MSFC')); gap = np.abs(md['lam_min'] - mn['lam_min']); post = t >= SUST_REL
    suf = np.logical_and.accumulate((gap <= 0.01)[::-1])[::-1]; idx2 = np.where(post & suf)[0]
    q3n[d]['metric_gap_vs_nominal_at_release'] = float(gap[np.argmin(np.abs(t - SUST_REL))])
    q3n[d]['time_after_release_until_metric_within_1pct_of_nominal_s'] = float(t[idx2[0]] - SUST_REL) if len(idx2) else None
    q3n[d]['max_metric_gap_vs_nominal_during_hold'] = float(gap[(t >= ONSET) & (t < SUST_REL)].max())
    q3n[d]['max_metric_gap_vs_nominal_after_release'] = float(gap[post].max())
    q3n[d]['w_at_release_m_s'] = float(md['w'][np.argmin(np.abs(t - SUST_REL))]); q3n[d]['w_nominal_at_release_m_s'] = float(mn['w'][np.argmin(np.abs(t - SUST_REL))])
# engagement windows for the frozen-observer receipts too
for key in ('nom-MSFC', 'tan-MSFC', 'pulse-MSFC'):
    q3[key]['engagement_windows'] = [engagement(mem(L5(key)), thr) for thr in (0.99, 0.95, 0.90)]
out['q3_closed_loop_memory_state_NO-v3'] = q3n
print(json.dumps(out, indent=1, default=float))
