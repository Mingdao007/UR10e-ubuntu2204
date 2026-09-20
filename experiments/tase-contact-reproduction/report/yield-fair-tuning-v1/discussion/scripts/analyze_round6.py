"""Round-6 read-only post-processing for the fair-tuning selection contract.

Inputs: /tmp/yfp6/*.npz (rows-only extracts, extract_rows_v6.py) and /tmp/yfp5/*.npz (round-5
cache, extract_ticks_v5.py), plus retained results/refinement JSON for stored numbers.
Output: JSON on stdout (retained as data/round6-analysis.json).  Nothing is re-run.
All clocks are the PATH clock; sustained release ends at 35.5 s, pulse at 20.5 s.
The candidate objective evaluated here is a proposal; evaluating it on development pairs is
a demonstration of its behaviour, not a pre-registration on those cells.
"""
import json, math, sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[4]
C6, C5 = '/tmp/yfp6', '/tmp/yfp5'
L6 = lambda k: np.load(f'{C6}/{k}.npz')
L5 = lambda k: np.load(f'{C5}/{k}.npz')
FB, XB = 0.5, 0.002          # existing recovery bands (contact_yield_metrics.py)
ONSET, PULSE_REL, SUST_REL = 20.0, 20.5, 35.5
out = {}

def stored(A):
    return json.loads(str(A['stored_metrics'])) if 'stored_metrics' in A.files else None

def nominal_descriptors(A, force_key):
    t = A['t']; dt = float(A['dt']); fe = A[force_key]; load = A['load_true']
    n = len(t); ref = A['ref_progress'].sum() * dt; act = A['act_progress'].sum() * dt
    late = t >= 20.0
    return {
        'n_samples': int(n), 'dt_s': dt,
        'force_mae_n': float(np.mean(np.abs(fe))), 'force_mae_0_20_n': float(np.mean(np.abs(fe[~late]))),
        'force_mae_20_end_n': float(np.mean(np.abs(fe[late]))),
        'force_peak_n': float(load.max()), 'load_min_n': float(load.min()),
        'below_1n_s': float(np.sum(load < 1.0) * dt),
        'path_rmse_mm': float(np.sqrt(np.mean(A['path_err'] ** 2)) * 1e3),
        'orientation_rmse_rad': float(np.sqrt(np.mean(A['orient_err'] ** 2))),
        'orientation_rmse_deg': float(np.degrees(np.sqrt(np.mean(A['orient_err'] ** 2)))),
        'normal_estimation_rmse_deg': float(np.degrees(np.sqrt(np.mean(A['n_err'] ** 2)))) if 'n_err' in A.files else None,
        'progress_ratio': float(act / ref), 'saturation_s': float(A['saturated'].sum() * dt), 'qp_s': float(A['qp'].sum() * dt),
    }

# ------------------------------------------------------------------ 1. nominal recomputation vs stored metrics
nom_keys = {'SFC/NO-v3': 'no3-nom-SFC', 'DSFC/NO-v3': 'no3-nom-DSFC', 'MSFC/NO-v3': 'no3-nom-MSFC',
            'SFC/legacy': 'leg-nom-SFC', 'DSFC/legacy': 'leg-nom-DSFC', 'MSFC/legacy': 'leg-nom-MSFC',
            'DSFC/NO-v3/along_10deg': 'no3-along-DSFC', 'DSFC/NO-v3/across_10deg': 'no3-across-DSFC',
            'DSFC/NO-v3/strong_surface': 'no3-strong-DSFC'}
sec = {}
for name, k in nom_keys.items():
    A = L6(k); d = nominal_descriptors(A, 'force_error'); s = stored(A)
    cmp = {'force_mae_n': (d['force_mae_n'], s['force_mae_n']), 'path_rmse_m': (d['path_rmse_mm'] * 1e-3, s['path_rmse_m']),
           'orientation_rmse_rad': (d['orientation_rmse_rad'], s['orientation_rmse_rad']),
           'progress_ratio': (d['progress_ratio'], s['progress_ratio']), 'force_peak_n': (d['force_peak_n'], s['force_peak_n'])}
    d['stored_vs_recomputed_max_abs_diff'] = max(abs(a - b) for a, b in cmp.values())
    d['receipt_sha256'] = str(A['sha256']); d['preparation'] = str(A['preparation'])
    d['estimator_parameters'] = json.loads(str(A['estimator_parameters']))
    d['law_parameters'] = json.loads(str(A['parameters']))
    sec[name] = d
out['nominal_recomputed'] = sec

seeds = {m: sec[f'{m}/NO-v3'] for m in ('SFC', 'DSFC', 'MSFC')}
legacy = {m: sec[f'{m}/legacy'] for m in ('SFC', 'DSFC', 'MSFC')}
def envelope_of(group, key, worst):
    vals = {m: group[m][key] for m in group}; pick = (max if worst == 'max' else min)(vals, key=vals.get)
    return {'value': vals[pick], 'set_by': pick, 'per_method': vals}
out['common_module_tradeoff'] = {
    'NO-v3_seed_envelope': {
        'path_rmse_mm_max': envelope_of(seeds, 'path_rmse_mm', 'max'), 'progress_min': envelope_of(seeds, 'progress_ratio', 'min'),
        'orientation_rmse_deg_max': envelope_of(seeds, 'orientation_rmse_deg', 'max'), 'orientation_rmse_rad_max': envelope_of(seeds, 'orientation_rmse_rad', 'max'),
        'force_mae_n_max': envelope_of(seeds, 'force_mae_n', 'max'), 'force_peak_n_max': envelope_of(seeds, 'force_peak_n', 'max'),
        'force_mae_20_end_n': {m: seeds[m]['force_mae_20_end_n'] for m in seeds},
        'normal_estimation_rmse_deg': {m: seeds[m]['normal_estimation_rmse_deg'] for m in seeds}},
    'legacy_seed_envelope': {
        'path_rmse_mm': {m: legacy[m]['path_rmse_mm'] for m in legacy}, 'progress': {m: legacy[m]['progress_ratio'] for m in legacy},
        'orientation_rmse_deg': {m: legacy[m]['orientation_rmse_deg'] for m in legacy}, 'orientation_rmse_rad': {m: legacy[m]['orientation_rmse_rad'] for m in legacy},
        'force_mae_n': {m: legacy[m]['force_mae_n'] for m in legacy}, 'normal_estimation_rmse_deg': {m: legacy[m]['normal_estimation_rmse_deg'] for m in legacy}},
    'ratio_NO-v3_over_legacy': {
        'path_rmse': {m: seeds[m]['path_rmse_mm'] / legacy[m]['path_rmse_mm'] for m in seeds},
        'orientation_rmse': {m: seeds[m]['orientation_rmse_rad'] / legacy[m]['orientation_rmse_rad'] for m in seeds},
        'progress': {m: seeds[m]['progress_ratio'] / legacy[m]['progress_ratio'] for m in seeds}},
    'NO-v3_DSFC_factor_cells': {k: {q: sec[k][q] for q in ('path_rmse_mm', 'progress_ratio', 'orientation_rmse_deg', 'force_mae_n', 'normal_estimation_rmse_deg', 'force_peak_n')}
                                for k in ('DSFC/NO-v3', 'DSFC/NO-v3/along_10deg', 'DSFC/NO-v3/across_10deg', 'DSFC/NO-v3/strong_surface')},
}

# ------------------------------------------------------------------ 2. nominal resolution from existing 2 ms / 1 ms nominals
def resolution_pair(coarse, fine, fk):
    a = nominal_descriptors(coarse, fk); b = nominal_descriptors(fine, fk)
    return {q: {'2ms': a[q], '1ms': b[q], 'abs_diff': abs(a[q] - b[q])} for q in ('force_mae_n', 'path_rmse_mm', 'orientation_rmse_deg', 'progress_ratio', 'force_peak_n')}
res = {'frozen_MSFC_g50_nominal_2ms_vs_1ms_8sub': resolution_pair(L5('nom-MSFC'), L5('fine-nom-MSFC'), 'row_force_error'),
       'frozen_MSFC_identity_nominal_2ms_vs_1ms_8sub': resolution_pair(L5('nom-MSFCid'), L5('fine-nom-MSFCid'), 'row_force_error')}
no3 = json.loads((ROOT / 'report/yield-normal-v3/results.json').read_text())
no3ref = json.loads((ROOT / 'report/yield-normal-v3/refinement.json').read_text())
c = next(r for r in no3['rows'] if r['id'] == 'combined-mild-across')['metrics']
f = next(r for r in no3ref['runs'] if r['id'] == 'combined-mild-across')
res['NO-v3_DSFC_across_prior_nominal_2ms_vs_1ms_8sub_reported'] = {
    'force_mae_n': {'2ms': c['force_mae_n'], '1ms': f['metrics']['force_mae_n'], 'abs_diff': abs(c['force_mae_n'] - f['metrics']['force_mae_n'])},
    'path_rmse_mm': {'2ms': c['path_rmse_m'] * 1e3, '1ms': f['metrics']['path_rmse_m'] * 1e3, 'abs_diff': abs(c['path_rmse_m'] - f['metrics']['path_rmse_m']) * 1e3},
    'orientation_rmse_deg': {'2ms': math.degrees(c['orientation_rmse_rad']), '1ms': math.degrees(f['metrics']['orientation_rmse_rad']), 'abs_diff': math.degrees(abs(c['orientation_rmse_rad'] - f['metrics']['orientation_rmse_rad']))},
    'progress_ratio': {'2ms': c['progress_ratio'], '1ms': f['metrics']['progress_ratio'], 'abs_diff': abs(c['progress_ratio'] - f['metrics']['progress_ratio'])},
    'pointwise_max': {'force_n': f['refinement']['force_error_max_n'], 'position_mm': f['refinement']['position_max_m'] * 1e3, 'orientation_rad': f['refinement']['orientation_error_max_rad']},
    'class': 'R: reported numbers, receipts not re-opened here'}
out['nominal_resolution_existing'] = res

# ------------------------------------------------------------------ 3. candidate objective on existing pairs
def recovery(t, d, dl, pb=XB, fb=FB, rel=SUST_REL):
    post = t >= rel; good = (d <= pb) & (np.abs(dl) <= fb)
    suf = np.logical_and.accumulate(good[::-1])[::-1]
    idx = np.where(post & suf & (t <= t[-1] - .1))[0]
    return float(t[idx[0]] - rel) if len(idx) else None

def objective(P, N, rel, fb=FB, xb=XB, load_from='onset'):
    t = P['t']; assert np.allclose(t, N['t']); dt = float(P['dt'])
    dl = np.abs(P['load_true'] - N['load_true']); dx = np.linalg.norm(P['row_pos'] - N['row_pos'], axis=1)
    ml = t >= (ONSET if load_from == 'onset' else rel); mp = t >= rel; hold = (t >= ONSET) & (t < rel)
    jl = float(dl[ml].sum() * dt / fb); jp = float(dx[mp].sum() * dt / xb)
    return {'J_s': jl + jp, 'J_load_s': jl, 'J_path_s': jp,
            'load_hold_component_s': float(dl[hold].sum() * dt / fb), 'load_post_component_s': float(dl[mp].sum() * dt / fb),
            'yield_integral_hold_mm_s': float(dx[hold].sum() * dt * 1e3), 'yield_peak_mm': float(dx[hold].max() * 1e3),
            'post_release_max_distance_mm': float(dx[mp].max() * 1e3), 'post_release_max_abs_delta_load_n': float(dl[mp].max()),
            'true_load_min_n': float(P['load_true'][t >= ONSET - 0.5].min()), 'true_load_max_n': float(P['load_true'][t >= ONSET - 0.5].max()),
            'below_1n_s': float(np.sum(P['load_true'] < 1.0) * dt),
            'recovery_s_reproduced': recovery(t, dx, P['load_true'] - N['load_true'], rel=rel),
            'disturbed_progress_ratio': float(P['act_progress'].sum() / P['ref_progress'].sum()),
            'disturbed_orientation_rmse_deg': float(np.degrees(np.sqrt(np.mean(P['orient_err'] ** 2)))),
            'disturbed_saturation_s': float(P['saturated'].sum() * dt)}

pairs = {
    'NO-v3 normal hold': {'SFC': (L6, 'no3-nrm-SFC', 'no3-nom-SFC'), 'DSFC': (L6, 'no3-nrm-DSFC', 'no3-nom-DSFC'), 'MSFC': (L6, 'no3-nrm-MSFC', 'no3-nom-MSFC')},
    'NO-v3 tangent hold': {'SFC': (L6, 'no3-tan-SFC', 'no3-nom-SFC'), 'DSFC': (L6, 'no3-tan-DSFC', 'no3-nom-DSFC'), 'MSFC': (L6, 'no3-tan-MSFC', 'no3-nom-MSFC')},
    'frozen tangent hold': {'SFC': (L5, 'tan-SFC', 'nom-SFC'), 'DSFC': (L5, 'tan-DSFC', 'nom-DSFC'), 'MSFC': (L5, 'tan-MSFC', 'nom-MSFC'), 'MSFC-identity': (L5, 'tan-MSFCid', 'nom-MSFCid')},
    'frozen pulse 2 ms': {'SFC': (L5, 'pulse-SFC', 'nom-SFC'), 'DSFC': (L5, 'pulse-DSFC', 'nom-DSFC'), 'MSFC': (L5, 'pulse-MSFC', 'nom-MSFC'), 'MSFC-identity': (L5, 'pulse-MSFCid', 'nom-MSFCid')},
    'frozen pulse 1 ms': {'MSFC': (L5, 'fine-pulse-MSFC', 'fine-nom-MSFC'), 'MSFC-identity': (L5, 'fine-pulse-MSFCid', 'fine-nom-MSFCid')},
}
rels = {'NO-v3 normal hold': SUST_REL, 'NO-v3 tangent hold': SUST_REL, 'frozen tangent hold': SUST_REL, 'frozen pulse 2 ms': PULSE_REL, 'frozen pulse 1 ms': PULSE_REL}
obj = {}; sweep = {}
for scen, arms in pairs.items():
    obj[scen] = {}; sweep[scen] = {}
    for arm, (Lf, pk, nk) in arms.items():
        P, N = Lf(pk), Lf(nk)
        obj[scen][arm] = objective(P, N, rels[scen])
        obj[scen][arm]['J_post_only_s'] = objective(P, N, rels[scen], load_from='release')['J_s']
        obj[scen][arm]['receipts'] = {'disturbed': str(P['sha256']), 'nominal': str(N['sha256'])}
        sweep[scen][arm] = {f'F{fb}_X{int(xb*1e3)}mm': objective(P, N, rels[scen], fb, xb)['J_s'] for fb in (0.3, 0.4, 0.5, 1.0) for xb in (0.001, 0.002, 0.003)}
def ordering(d):
    return [k for k, _ in sorted(d.items(), key=lambda kv: kv[1])]
out['objective_definition'] = {
    'J_s': 'integral over PATH time of |load_disturbed - load_nominal| / 0.5 N from intervention onset (20.0 s) to the end of PATH, plus integral of ||x_disturbed - x_nominal|| / 2 mm from release to the end of PATH; unit seconds (band-seconds); matched same-method, same-material, same-preparation nominal; nonnegative; no threshold crossing',
    'reported_alongside_unchanged': ['compare_pair recovery_s at 2 mm / 0.5 N', 'yield_peak', 'recoil', 'residual', 'true load min/max', 'saturation s', 'QP s'],
    'feasibility_is_separate': 'nominal bands and disturbed guards gate eligibility; they are not summed into J'}
out['objective_on_existing_pairs'] = obj
out['objective_band_weight_sweep'] = {scen: {w: ordering({arm: sweep[scen][arm][w] for arm in sweep[scen]}) for w in next(iter(sweep[scen].values()))} for scen in sweep}
out['objective_band_weight_sweep_values'] = sweep
out['recovery_ordering_vs_objective_ordering'] = {scen: {'recovery': ordering({a: (obj[scen][a]['recovery_s_reproduced'] if obj[scen][a]['recovery_s_reproduced'] is not None else 1e9) for a in obj[scen]}),
                                                         'J': ordering({a: obj[scen][a]['J_s'] for a in obj[scen]})} for scen in obj}

# ------------------------------------------------------------------ 4. step sensitivity of J and the resolution-aware criterion (pulse pair)
r_on = abs(obj['frozen pulse 1 ms']['MSFC']['J_s'] - obj['frozen pulse 2 ms']['MSFC']['J_s'])
r_id = abs(obj['frozen pulse 1 ms']['MSFC-identity']['J_s'] - obj['frozen pulse 2 ms']['MSFC-identity']['J_s'])
diff2 = obj['frozen pulse 2 ms']['MSFC']['J_s'] - obj['frozen pulse 2 ms']['MSFC-identity']['J_s']
diff1 = obj['frozen pulse 1 ms']['MSFC']['J_s'] - obj['frozen pulse 1 ms']['MSFC-identity']['J_s']
def res_crit(name, key):
    ro = abs(obj['frozen pulse 1 ms']['MSFC'][key] - obj['frozen pulse 2 ms']['MSFC'][key])
    ri = abs(obj['frozen pulse 1 ms']['MSFC-identity'][key] - obj['frozen pulse 2 ms']['MSFC-identity'][key])
    d2 = obj['frozen pulse 2 ms']['MSFC'][key] - obj['frozen pulse 2 ms']['MSFC-identity'][key]
    d1 = obj['frozen pulse 1 ms']['MSFC'][key] - obj['frozen pulse 2 ms']['MSFC-identity'][key] * 0 + obj['frozen pulse 1 ms']['MSFC'][key] - obj['frozen pulse 1 ms']['MSFC-identity'][key]
    d1 = obj['frozen pulse 1 ms']['MSFC'][key] - obj['frozen pulse 1 ms']['MSFC-identity'][key]
    return {'descriptor': name, 'r_MSFC': ro, 'r_identity': ri, 'on_minus_identity_2ms': d2, 'on_minus_identity_1ms': d1,
            'resolved_at_2ms_(|diff|>r_on+r_id)': abs(d2) > ro + ri, 'same_sign_both_steps': (d2 > 0) == (d1 > 0)}
out['resolution_aware_criterion_demo_pulse'] = {
    'note': 'FP-v1 refinement halves controller dt and plant step together; this is one combined check, not the two separated checks recommended',
    'J_s': res_crit('J_s', 'J_s'), 'J_post_only_s': res_crit('J_post_only_s', 'J_post_only_s'), 'J_load_s': res_crit('J_load_s', 'J_load_s'), 'J_path_s': res_crit('J_path_s', 'J_path_s'),
    'true_load_max_n': res_crit('true_load_max_n', 'true_load_max_n'), 'recovery_s': res_crit('recovery_s_reproduced', 'recovery_s_reproduced')}

# ------------------------------------------------------------------ 5. proposer bounds vs seeds
MB, MUB, GB = (4.0, 16.0), (40.0, 2500.0), (0.005, 0.2)
seeds_triple = {'SFC': (4.0, 393.0, 0.052126826121414976), 'DSFC': (4.0, 310.66177089084647, 0.0642), 'MSFC': (4.0, 1228.1391393367705, 0.08583341909109758)}
unit = lambda m, mu, g: ((m - MB[0]) / (MB[1] - MB[0]), (math.log(mu) - math.log(MUB[0])) / (math.log(MUB[1]) - math.log(MUB[0])), (math.log(g) - math.log(GB[0])) / (math.log(GB[1]) - math.log(GB[0])))
out['proposer_bounds_check'] = {
    'bounds': {'m': MB, 'mu': MUB, 'g': GB}, 'seed_unit_cube_coordinates': {k: unit(*v) for k, v in seeds_triple.items()},
    'all_seeds_inside': all(MB[0] <= m <= MB[1] and MUB[0] <= mu <= MUB[1] and GB[0] <= g <= GB[1] for m, mu, g in seeds_triple.values()),
    'all_seeds_on_m_lower_boundary': all(m == MB[0] for m, _, _ in seeds_triple.values()),
    'mu_headroom_above_msfc_seed': MUB[1] / seeds_triple['MSFC'][1], 'g_headroom_above_msfc_seed': GB[1] / seeds_triple['MSFC'][2],
    'legacy_tuner_dsfc_fixed_a_p': {'a': 1.2, 'p': 0.1}, 'current_fixed_a_p': {'a': 0.05, 'p': 0.5},
    'source': 'config/yield_fair_tuning_v1.json and tools/yield_contact_tuner.py at HEAD bc2d016a; report/yield-coefficient-equivalence-v1/tuner-bounds.json'}

# ------------------------------------------------------------------ 6. memory time scale and scenario geometry
msfc = json.loads(str(L6('no3-nom-MSFC')['parameters']))
out['msfc_memory_time_scale'] = {
    'tau_force_s': msfc['tau_force_s'], 'tau_recovery_s': msfc['tau_recovery_s'], 'kappa_per_n2_s': msfc['kappa_per_n2_s'], 'force_scale_n': msfc['force_scale_n'],
    'minimum_metric_eigenvalue': msfc['minimum_metric_eigenvalue'],
    'time_to_5_percent_of_structure_after_excitation_s': 3.0 * msfc['tau_recovery_s'],
    'reading': 'h and S are first-order low-pass states with about 0.2 s time constants; the metric relaxes toward its nominal steady state within about one second of any excitation. There is no long-horizon memory state to be cold or warm across cycles; the only existing state-preparation factor is the runner preparation flag (cold: entry only; warm: 2 s stationary baseline before entry).'}
out['scenario_geometry'] = {
    'sustained_release_oblique': {'amplitude_n': 2.5, 'axis': '(outward + tangent)/sqrt(2) at the true contact point', 'outward_component_n': 2.5 / math.sqrt(2), 'tangent_component_n': 2.5 / math.sqrt(2),
        'schedule_path_s': {'rise': [20.0, 20.5], 'hold': [20.5, 30.5], 'release_ramp': [30.5, 35.5], 'post_release_window_s': 2 * math.pi / 0.1 - 35.5},
        'quasi_static_tangential_yield_mm_Kp120': 2.5 / math.sqrt(2) / 120.0 * 1e3, 'normal_penetration_change_mm': {'stiff_8000': 2.5 / math.sqrt(2) / 8000 * 1e3, 'compliant_2000': 2.5 / math.sqrt(2) / 2000 * 1e3},
        'never_run_in_any_full_cycle_study': True},
    'short_pulse_oblique': {'amplitude_n': 3.0, 'outward_component_n': 3 / math.sqrt(2), 'schedule_path_s': {'pulse': [20.0, 20.5]}, 'post_release_window_s': 2 * math.pi / 0.1 - 20.5}}

# ------------------------------------------------------------------ 7. cost from retained manifests
man = [('FP-v1 four pulse cells (2 ms/8 sub)', 4, 2, '2026-09-20T00:40:30.347933', '2026-09-20T00:43:03.415072'),
       ('NO-v3 nine cells (2 ms/8 sub)', 9, 3, '2026-09-19T23:50:42.298289', '2026-09-19T23:54:42.369365'),
       ('OT-v1 six cells (2 ms/8 sub)', 6, 3, '2026-09-20T00:03:14.855342', '2026-09-20T00:05:55.769805'),
       ('FP-v1 refinement four runs (1 ms/8 sub)', 4, 2, '2026-09-20T00:51:30.086535', '2026-09-20T00:59:01.357775'),
       ('NO-v3 refinement two runs (1 ms/8 sub)', 2, 2, '2026-09-19T23:56:13.396406', '2026-09-19T23:58:46.964024')]
from datetime import datetime
cost = []
for name, cells, workers, a, b in man:
    el = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    cost.append({'manifest': name, 'cells': cells, 'workers': workers, 'elapsed_s': el, 'worker_seconds_per_trial_upper_bound': el * min(workers, cells) / cells})
coarse = np.mean([c['worker_seconds_per_trial_upper_bound'] for c in cost[:3]]); fine = np.mean([c['worker_seconds_per_trial_upper_bound'] for c in cost[3:]])
out['cost_from_manifests'] = {'rows': cost, 'assumed_worker_s_per_trial_2ms': float(coarse), 'assumed_worker_s_per_trial_1ms': float(fine),
    'note': 'upper bounds from wall time and worker count; parallel wall time is not realtime evidence; 2 ms/16-substep cost is not retained and is assumed <= 1 ms cost',
    'training': {'trials': 3 * 24 * 2, 'worker_hours_2ms': 3 * 24 * 2 * coarse / 3600, 'sequential_units_per_method': 24,
                 'wall_hours_if_three_methods_parallel_and_pair_members_parallel': 24 * coarse / 3600},
    'training_cell_numerical_checks': {'trials': 3 * 2 * 2, 'worker_hours': 3 * 2 * 2 * fine / 3600, 'definition': 'per incumbent: (controller 1 ms, plant 0.25 ms) pair and (controller 2 ms, plant 0.125 ms) pair'},
    'training_cell_ablation_checks_optional': {'trials': 2 * 2, 'definition': 'MSFC-identity at MSFC incumbent triple and SFC_RADIAL at SFC incumbent triple, one pair each at the training cell', 'worker_hours': 4 * coarse / 3600}}

# ------------------------------------------------------------------ 8. holdout block counts
arms5 = ['SFC', 'DSFC', 'MSFC', 'MSFC-identity(at MSFC incumbent)', 'SFC_RADIAL(at SFC incumbent)']
blocks = [
    {'block': 'H1 direction/shape transfer', 'material': 'stiff_low_mu', 'surface': 'mild (0.8,0.4)', 'prior': 'approach', 'preparation': 'cold',
     'scenarios': ['sustained_release_normal', 'sustained_release_tangent', 'short_pulse_normal', 'short_pulse_tangent', 'short_pulse_oblique'], 'arms': 5,
     'nominals_new': 2, 'nominal_note': 'tuned-arm nominals reused from training incumbents by hash; ablation arms need 2 new nominals', 'status': 'stiff normal/tangent/pulse-oblique scenarios were viewed in development with other parameters; pulse normal/tangent never run'},
    {'block': 'H2 material transfer', 'material': 'compliant_high_mu', 'surface': 'mild (0.8,0.4)', 'prior': 'approach', 'preparation': 'cold',
     'scenarios': ['sustained_release_normal', 'sustained_release_tangent', 'sustained_release_oblique', 'short_pulse_normal', 'short_pulse_tangent', 'short_pulse_oblique'], 'arms': 5, 'nominals_new': 5,
     'status': 'NO-v3 observer has never been run on this material; legacy and frozen observers were (SE-v1, TR-v1)'},
    {'block': 'H3 unknown-surface (strong curvature)', 'material': 'stiff_low_mu', 'surface': 'strong (6,8)', 'prior': 'approach', 'preparation': 'cold',
     'scenarios': ['sustained_release_oblique', 'short_pulse_oblique'], 'arms': 5, 'nominals_new': 5, 'status': 'only DSFC seed nominal exists under NO-v3 (progress 0.745)'},
    {'block': 'H4 prior uncertainty', 'material': 'stiff_low_mu', 'surface': 'mild (0.8,0.4)', 'prior': 'along_10deg and across_10deg', 'preparation': 'cold',
     'scenarios': ['sustained_release_oblique'], 'arms': 5, 'nominals_new': 10, 'status': 'only DSFC seed nominals exist under NO-v3; two priors, so cells = 2 x scenarios'},
    {'block': 'H5 warm preparation', 'material': 'stiff_low_mu', 'surface': 'mild (0.8,0.4)', 'prior': 'approach', 'preparation': 'warm (2 s stationary baseline)',
     'scenarios': ['sustained_release_oblique', 'short_pulse_oblique'], 'arms': 5, 'nominals_new': 5, 'status': 'runner flag exists; never used in a full-cycle study; matched nominal must also be warm'},
]
tot = 0
for b in blocks:
    mult = 2 if 'and' in b['prior'] else 1
    b['disturbed_trials'] = len(b['scenarios']) * b['arms'] * mult; b['trials'] = b['disturbed_trials'] + b['nominals_new']; tot += b['trials']
out['holdout_blocks'] = {'blocks': blocks, 'total_trials': tot, 'worker_hours_2ms': tot * coarse / 3600,
    'claim_conditional_numerical_checks': {'rule': 'every holdout cell on which a cross-arm benefit is claimed needs both separated checks for both compared arms (4 trials per arm per cell); cells without checks carry no claim',
        'upper_bound_trials_if_every_block_representative_checked_for_three_tuned_arms': 5 * 3 * 4, 'worker_hours_upper': 5 * 3 * 4 * fine / 3600},
    'excluded_by_design': ['training cell stiff/mild/approach/cold/sustained_release_oblique', 'intervention onset time (not a runner parameter; would need a protocol/runtime edit)', 'new surfaces beyond SE-v1 (6,8)', 'sensor noise models', 'any physical repeat']}


# ------------------------------------------------------------------ 9. attitude by window (guard design) and attitude-deviation integrals
def attitude_windows(P, N, rel):
    t = P['t']; dt = float(P['dt']); a, b = P['orient_err'], N['orient_err']
    win = {'pre_0_20': (t >= 0) & (t < ONSET), 'hold_onset_to_release': (t >= ONSET) & (t < rel), 'post_release': t >= rel, 'full': t >= 0}
    rms = lambda x, m: float(np.degrees(np.sqrt(np.mean(x[m] ** 2))))
    return {w: {'disturbed_rms_deg': rms(a, m), 'nominal_rms_deg': rms(b, m), 'max_abs_deviation_deg': float(np.degrees(np.abs(a[m] - b[m]).max())),
                'deviation_integral_deg_s': float(np.degrees(np.abs(a[m] - b[m]).sum() * dt))} for w, m in win.items()}
att = {}
for scen, arms in pairs.items():
    att[scen] = {}
    for arm, (Lf, pk, nk) in arms.items():
        att[scen][arm] = attitude_windows(Lf(pk), Lf(nk), rels[scen])
out['attitude_by_window'] = att
out['attitude_guard_candidates'] = {
    'post_release_attitude_rms_deg_max_over_NO-v3_pairs': max(att[s][a]['post_release']['disturbed_rms_deg'] for s in ('NO-v3 normal hold', 'NO-v3 tangent hold') for a in att[s]),
    'post_release_max_abs_deviation_deg_over_NO-v3_pairs': max(att[s][a]['post_release']['max_abs_deviation_deg'] for s in ('NO-v3 normal hold', 'NO-v3 tangent hold') for a in att[s]),
    'hold_attitude_rms_deg_max_over_NO-v3_pairs': max(att[s][a]['hold_onset_to_release']['disturbed_rms_deg'] for s in ('NO-v3 normal hold', 'NO-v3 tangent hold') for a in att[s]),
    'nominal_envelope_deg': out['common_module_tradeoff']['NO-v3_seed_envelope']['orientation_rmse_deg_max']['value']}

# ------------------------------------------------------------------ 10. paired resolution criterion (both arms at both settings)
def paired(key):
    d2 = obj['frozen pulse 2 ms']['MSFC'][key] - obj['frozen pulse 2 ms']['MSFC-identity'][key]
    d1 = obj['frozen pulse 1 ms']['MSFC'][key] - obj['frozen pulse 1 ms']['MSFC-identity'][key]
    diffs = [d2, d1]; mn = min(abs(x) for x in diffs); var = max(diffs) - min(diffs)
    return {'cross_arm_diff_by_setting': {'2ms_0.25ms': d2, '1ms_0.125ms': d1}, 'same_sign': (d2 > 0) == (d1 > 0), 'min_abs_diff': mn, 'variation_across_settings': var,
            'resolved_paired_(same_sign_and_min_abs>variation)': ((d2 > 0) == (d1 > 0)) and mn > var}
out['paired_resolution_criterion_demo_pulse'] = {k: paired(k) for k in ('J_s', 'J_post_only_s', 'J_load_s', 'J_path_s', 'true_load_max_n', 'recovery_s_reproduced')}

# ------------------------------------------------------------------ 11. band rule applied to already-reported pointwise step differences (disclosure)
known = [
    ('FP-v1 MSFC g50 pulse, 2ms/0.25 -> 1ms/0.125 (both halved)', 0.126081, 0.032736, 'R: refinement/README.md'),
    ('FP-v1 MSFC-identity pulse, both halved', 0.123408, 0.030754, 'R: refinement/README.md'),
    ('NO-v3 DSFC across-prior nominal, both halved', 0.194665, 0.441691, 'R: refinement.json'),
    ('OT-v1/DC-v1 SFC NO-v3 normal hold, both halved', 1.005, 1.002, 'R: OT-v1 README'),
    ('GM-v1 MSFC g100 normal hold, plant 0.5 -> 0.25 ms at 2 ms', 3.602706, 0.672130, 'R: GM-v1 results.md'),
    ('GM-v1 MSFC g50 normal hold, plant 0.5 -> 0.25 ms at 2 ms', 0.043874, 0.012494, 'R: GM-v1 results.md'),
    ('common-fix SFC tangent hold, plant 0.5 -> 0.25 ms at 2 ms', 0.036016, 0.017081, 'R: common-fix.md'),
]
out['band_rule_disclosure'] = {'rule': 'pointwise |load difference| <= 0.5 N and |TCP difference| <= 2 mm between base and each refined setting, over the full PATH; these are the existing recovery bands, not new numbers; declared after having seen the values below',
    'applied_to_reported_values': [{'case': c, 'max_force_diff_n': f, 'max_tcp_diff_mm': x, 'within_bands': (f <= 0.5 and x <= 2.0), 'source': src} for c, f, x, src in known]}


# ------------------------------------------------------------------ 12. three-term objective with the attitude band (0.05 rad = NO-v3 motion_rate_cap_rad_s x 1 s)
TB = 0.05
def j_att(P, N, tb=TB):
    t = P['t']; dt = float(P['dt']); m = t >= ONSET
    return float(np.abs(P['orient_err'][m] - N['orient_err'][m]).sum() * dt / tb)
j3 = {}; j3sweep = {}
for scen, arms in pairs.items():
    j3[scen] = {}; j3sweep[scen] = {}
    for arm, (Lf, pk, nk) in arms.items():
        P, N = Lf(pk), Lf(nk); ja = j_att(P, N); base = obj[scen][arm]
        j3[scen][arm] = {'J_att_s': ja, 'J3_s': base['J_s'] + ja, 'J_load_s': base['J_load_s'], 'J_path_s': base['J_path_s']}
        j3sweep[scen][arm] = {f'T{tb}': base['J_s'] + j_att(P, N, tb) for tb in (0.025, 0.05, 0.1)}
out['objective_three_term'] = {'attitude_band_rad': TB, 'attitude_band_source': 'NO-v3 observer motion_rate_cap_rad_s = 0.05 rad/s times one second; the attitude error the frozen observer version can remove in 1 s; not a data-fitted number',
    'values': j3, 'ordering_by_J3': {scen: ordering({a: j3[scen][a]['J3_s'] for a in j3[scen]}) for scen in j3},
    'attitude_band_sweep_ordering': {scen: {w: ordering({a: j3sweep[scen][a][w] for a in j3sweep[scen]}) for w in next(iter(j3sweep[scen].values()))} for scen in j3sweep}}
def paired3(key):
    d2 = j3['frozen pulse 2 ms']['MSFC'][key] - j3['frozen pulse 2 ms']['MSFC-identity'][key]
    d1 = j3['frozen pulse 1 ms']['MSFC'][key] - j3['frozen pulse 1 ms']['MSFC-identity'][key]
    mn = min(abs(d2), abs(d1)); var = abs(d2 - d1)
    return {'cross_arm_diff_by_setting': {'2ms_0.25ms': d2, '1ms_0.125ms': d1}, 'same_sign': (d2 > 0) == (d1 > 0), 'min_abs_diff': mn, 'variation_across_settings': var, 'resolved_paired': ((d2 > 0) == (d1 > 0)) and mn > var,
            'unpaired_r_MSFC': abs(j3['frozen pulse 1 ms']['MSFC'][key] - j3['frozen pulse 2 ms']['MSFC'][key]), 'unpaired_r_identity': abs(j3['frozen pulse 1 ms']['MSFC-identity'][key] - j3['frozen pulse 2 ms']['MSFC-identity'][key])}
out['paired_resolution_criterion_demo_pulse']['J3_s'] = paired3('J3_s'); out['paired_resolution_criterion_demo_pulse']['J_att_s'] = paired3('J_att_s')

print(json.dumps(out, indent=1, default=float))
