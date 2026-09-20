"""Round-5 read-only tick extractor for contact-yield full-state receipts.

Extends the round-4 extractor (report/yield-normal-v3/discussion/scripts/extract_ticks_v3.py)
with the native law input/state/output and the 22-slot law snapshot, so memory activation,
law dynamics and band margins can be post-processed without re-running anything.
`t` is the PATH clock from `rows`; `sample_t` is the observation clock (entry included).
Usage: extract_ticks_v5.py <receipt.json.gz> <out.npz>
"""
import gzip, json, sys, hashlib
import numpy as np

src, dst = sys.argv[1], sys.argv[2]
raw = open(src, 'rb').read(); sha = hashlib.sha256(raw).hexdigest()
D = json.loads(gzip.decompress(raw)); del raw
rows = D['rows']; recs = D['records']
path = [r for r in recs if r['reference']['phase'] == 'path']
entry = [r for r in recs if r['reference']['phase'] == 'entry']
assert len(rows) == len(path), (len(rows), len(path))
get = lambda seq, k: np.asarray([x[k] for x in seq], dtype=float)
res = lambda seq, k, d=None: np.asarray([r['result'].get(k, d) for r in seq], dtype=float)
obs = lambda seq, k: np.asarray([r['observation'][k] for r in seq], dtype=float)
def est(seq, k, d=False):
    return np.asarray([bool((r['result'].get('estimator') or {}).get(k, d)) for r in seq])
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
        prefix + 'law_force': res(seq, 'law_force_base_n'),
        prefix + 'law_vel': res(seq, 'law_velocity_base_m_s'),
        prefix + 'law_state': res(seq, 'law_state'),
        prefix + 'law_normal_vel': res(seq, 'law_normal_velocity_m_s'),
        prefix + 'law_tan': res(seq, 'law_tangent_velocity_m_s'),
        prefix + 'applied_twist': res(seq, 'applied_twist_base'),
        prefix + 'task_scale': res(seq, 'task_scale'),
        prefix + 'signed_load': res(seq, 'signed_normal_load_n'),
        prefix + 'force_error': res(seq, 'force_error_n'),
        prefix + 'normal_saturated': np.asarray([bool(r['result'].get('normal_saturated', False)) for r in seq]),
        prefix + 'tangent_saturation': res(seq, 'tangent_saturation_m_s', np.nan),
        prefix + 'sw_injection': res(seq, 'software_injection_base_n'),
        prefix + 'contact_gate': est(seq, 'contact_gate'),
        prefix + 'excitation_gate': est(seq, 'excitation_gate'),
        prefix + 'law22': np.asarray([r['controller_snapshot']['law22']['values'] for r in seq], dtype=float),
        prefix + 'f_true_post': np.asarray([r['simulator_snapshot']['true_wrench'][:3] for r in seq], dtype=float),
        prefix + 'f_sensed_post': np.asarray([r['simulator_snapshot']['sensed_force'] for r in seq], dtype=float),
    }
out = dict(
    sha256=np.array(sha), scenario=np.array(D['scenario']), method=np.array(D['method']), dt=np.array(D['dt_s']),
    parameters=np.array(json.dumps(D['identity_payload']['parameters'])),
    estimator_parameters=np.array(json.dumps(D['identity_payload']['estimator_parameters'])),
    settings=np.array(json.dumps(D['identity_payload']['settings'])),
    material=np.array(json.dumps(D['plant_identity_payload']['material'])),
    t=get(rows, 'time_s'), n_true=get(rows, 'true_inward_normal'), n_est_row=get(rows, 'normal_estimate'),
    load_true=get(rows, 'true_normal_load_n'), f_ext=get(rows, 'external_force_base_n'),
    ref_pos=get(rows, 'reference_position_m'), path_err=get(rows, 'path_error_m'), row_pos=get(rows, 'position_m'),
    row_force_error=get(rows, 'force_error_n'),
    orient_err=get(rows, 'orientation_error_rad'), n_err=get(rows, 'normal_estimation_error_rad'),
    ref_progress=get(rows, 'reference_progress_m_s'), act_progress=get(rows, 'actual_progress_m_s'),
    qp=np.asarray([x['qp_intervention'] for x in rows], dtype=bool),
    saturated=np.asarray([x['saturated'] for x in rows], dtype=bool),
)
out.update(block(path, ''))
out.update(block(entry, 'e_'))
np.savez_compressed(dst, **out)
print(dst, len(rows), len(entry), sha[:12], flush=True)
