"""Extract compact per-tick arrays from a NO-v3 full-state receipt.  Read-only.

Writes a .npz cache (not a retained artifact) with PATH ticks and the 1 s entry.
`t` is the PATH clock from `rows`; `sample_t` is the observation clock (entry included).
Evaluator truth (true normal, true load, external force) comes from `rows`;
controller-side quantities come from `records[*].result`/`observation`.
Usage: extract_ticks_v3.py <receipt.json.gz> <out.npz>
"""
import gzip, json, sys, hashlib
import numpy as np

src, dst = sys.argv[1], sys.argv[2]
raw = open(src, 'rb').read(); sha = hashlib.sha256(raw).hexdigest()
D = json.loads(gzip.decompress(raw)); del raw
rows = D['rows']
recs = D['records']
path = [r for r in recs if r['reference']['phase'] == 'path']
entry = [r for r in recs if r['reference']['phase'] == 'entry']
assert len(rows) == len(path), (len(rows), len(path))
get = lambda seq, k: np.asarray([x[k] for x in seq], dtype=float)
res = lambda seq, k: np.asarray([r['result'][k] for r in seq], dtype=float)
obs = lambda seq, k: np.asarray([r['observation'][k] for r in seq], dtype=float)
est = lambda seq, k, d=None: np.asarray([r['result']['estimator'].get(k, d) for r in seq])
def cpres(seq):
    return np.asarray([np.nan if r['result']['estimator'].get('coplanarity_residual') is None
                       else r['result']['estimator']['coplanarity_residual'] for r in seq], dtype=float)
def block(seq, prefix):
    return {
        prefix + 'sample_t': res(seq, 'sample_time_s'),
        prefix + 'pos': obs(seq, 'position_m'),
        prefix + 'v': obs(seq, 'linear_velocity_base_m_s'),
        prefix + 'f_raw': obs(seq, 'raw_force_base_n'),
        prefix + 'f_filt': res(seq, 'filtered_force_base_n'),
        prefix + 'n_est': res(seq, 'inward_normal_base'),
        prefix + 'ref_vel': res(seq, 'reference_velocity_base_m_s'),
        prefix + 'path_err_base': res(seq, 'path_error_base_m'),
        prefix + 'offset': res(seq, 'offset_base_m'),
        prefix + 'normal_speed': res(seq, 'normal_speed_m_s'),
        prefix + 'law_tan': res(seq, 'law_tangent_velocity_m_s'),
        prefix + 'applied_twist': res(seq, 'applied_twist_base'),
        prefix + 'task_scale': res(seq, 'task_scale'),
        prefix + 'signed_load': res(seq, 'signed_normal_load_n'),
        prefix + 'contact_gate': est(seq, 'contact_gate').astype(bool),
        prefix + 'excitation_gate': est(seq, 'excitation_gate').astype(bool),
        prefix + 'motion_applied': est(seq, 'motion_update_applied').astype(bool),
        prefix + 'cp_applied': est(seq, 'coplanarity_update_applied', False).astype(bool),
        prefix + 'cp_residual': cpres(seq),
        prefix + 'f_true_post': np.asarray([r['simulator_snapshot']['true_wrench'][:3] for r in seq], dtype=float),
        prefix + 'f_sensed_post': np.asarray([r['simulator_snapshot']['sensed_force'] for r in seq], dtype=float),
    }
out = dict(
    sha256=np.array(sha), scenario=np.array(D['scenario']), method=np.array(D['method']),
    surface=np.array(json.dumps(D['plant_identity_payload']['surface'])),
    approach=np.asarray(D['identity_payload']['approach_inward_base'], dtype=float),
    estimator_parameters=np.array(json.dumps(D['identity_payload']['estimator_parameters'])),
    settings=np.array(json.dumps(D['identity_payload']['settings'])),
    n0=np.asarray(D['initial_controller_snapshot']['normal_estimate']['inward_normal_base'], dtype=float),
    t=get(rows, 'time_s'), n_true=get(rows, 'true_inward_normal'), n_est_row=get(rows, 'normal_estimate'),
    load_true=get(rows, 'true_normal_load_n'), f_ext=get(rows, 'external_force_base_n'),
    ref_pos=get(rows, 'reference_position_m'), path_err=get(rows, 'path_error_m'),
    orient_err=get(rows, 'orientation_error_rad'), n_err=get(rows, 'normal_estimation_error_rad'),
    ref_progress=get(rows, 'reference_progress_m_s'), act_progress=get(rows, 'actual_progress_m_s'),
    qp=np.asarray([x['qp_intervention'] for x in rows], dtype=bool),
    saturated=np.asarray([x['saturated'] for x in rows], dtype=bool),
)
out.update(block(path, ''))
out.update(block(entry, 'e_'))
np.savez_compressed(dst, **out)
print(dst, len(rows), len(entry), sha[:12], flush=True)
