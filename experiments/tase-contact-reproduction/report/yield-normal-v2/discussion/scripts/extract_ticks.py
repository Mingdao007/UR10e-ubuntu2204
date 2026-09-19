"""Extract compact per-PATH-tick arrays from a retained full-state run receipt.

Read-only on the receipt.  Writes a small .npz cache (not a retained artifact).
Usage: extract_ticks.py <receipt.json.gz> <out.npz>
"""
import gzip, json, sys, hashlib
import numpy as np

src, dst = sys.argv[1], sys.argv[2]
sha = hashlib.sha256(open(src, 'rb').read()).hexdigest()
with gzip.open(src, 'rt') as f:
    D = json.load(f)
rows = D['rows']; recs = [r for r in D['records'] if r['result']['phase'] == 'path']
assert len(rows) == len(recs), (len(rows), len(recs))
get = lambda seq, k: np.asarray([x[k] for x in seq], dtype=float)
out = dict(
    sha256=np.array(sha), method=np.array(D['method']), scenario=np.array(D['scenario']),
    material=np.array(D['material']),
    approach=np.asarray(D['identity_payload']['approach_inward_base'], dtype=float),
    estimator_parameters=np.array(json.dumps(D['identity_payload']['estimator_parameters'])),
    t=get(rows, 'time_s'),
    n_true=get(rows, 'true_inward_normal'), n_est=get(rows, 'normal_estimate'),
    load_true=get(rows, 'true_normal_load_n'), f_ext=get(rows, 'external_force_base_n'),
    pos=get(rows, 'position_m'), ref_pos=get(rows, 'reference_position_m'),
    path_err=get(rows, 'path_error_m'), orient_err=get(rows, 'orientation_error_rad'),
    n_err=get(rows, 'normal_estimation_error_rad'),
    v=np.asarray([r['observation']['linear_velocity_base_m_s'] for r in recs], dtype=float),
    f_raw=np.asarray([r['observation']['raw_force_base_n'] for r in recs], dtype=float),
    f_filt=np.asarray([r['result']['filtered_force_base_n'] for r in recs], dtype=float),
    path_err_base=np.asarray([r['result']['path_error_base_m'] for r in recs], dtype=float),
    ref_vel=np.asarray([r['result']['reference_velocity_base_m_s'] for r in recs], dtype=float),
    law_tan=np.asarray([r['result']['law_tangent_velocity_m_s'] for r in recs], dtype=float),
    normal_speed=np.asarray([r['result']['normal_speed_m_s'] for r in recs], dtype=float),
    applied_twist=np.asarray([r['result']['applied_twist_base'] for r in recs], dtype=float),
    task_scale=np.asarray([r['result']['task_scale'] for r in recs], dtype=float),
    contact_gate=np.asarray([r['result']['estimator']['contact_gate'] for r in recs], dtype=bool),
    excitation_gate=np.asarray([r['result']['estimator']['excitation_gate'] for r in recs], dtype=bool),
    motion_applied=np.asarray([r['result']['estimator']['motion_update_applied'] for r in recs], dtype=bool),
    force_applied=np.asarray([r['result']['estimator']['force_bias_correction_applied'] for r in recs], dtype=bool),
    signed_load=np.asarray([r['result']['signed_normal_load_n'] for r in recs], dtype=float),
)
np.savez_compressed(dst, **out)
print(dst, len(rows), sha[:12])
