"""Round-6 read-only rows extractor for contact-yield full-state receipts.

Lighter than the round-5 extractor: only the evaluator `rows` block and the receipt
header (method, scenario, material, dt, preparation, identity payload, stored metrics)
are parsed.  The `records` block (full state) is not decoded.  `t` is the PATH clock.
Usage: extract_rows_v6.py <receipt.json.gz> <out.npz>
"""
import gzip, json, sys, hashlib
import numpy as np

src, dst = sys.argv[1], sys.argv[2]
raw = open(src, 'rb').read(); sha = hashlib.sha256(raw).hexdigest()
text = gzip.decompress(raw).decode('utf-8'); del raw
cut = text.find(',"records":[')
assert cut > 0, 'records block not found'
head = json.loads(text[:cut] + '}'); del text
rows = head['rows']
get = lambda k: np.asarray([x[k] for x in rows], dtype=float)
out = dict(
    sha256=np.array(sha), method=np.array(head['method']), scenario=np.array(head['scenario']),
    material=np.array(head['material']), dt=np.array(head['dt_s']), preparation=np.array(head['preparation']),
    duration=np.array(head['duration_s']), timeline=np.array(head['timeline']),
    identity=np.array(head['identity']),
    parameters=np.array(json.dumps(head['identity_payload']['parameters'])),
    estimator_parameters=np.array(json.dumps(head['identity_payload']['estimator_parameters'])),
    settings=np.array(json.dumps(head['identity_payload']['settings'])),
    plant_identity=np.array(json.dumps(head['plant_identity_payload'])),
    stored_metrics=np.array(json.dumps(head['metrics'])),
    t=get('time_s'), load_true=get('true_normal_load_n'), force_error=get('force_error_n'),
    path_err=get('path_error_m'), row_pos=get('position_m'), orient_err=get('orientation_error_rad'),
    n_err=get('normal_estimation_error_rad'), ref_progress=get('reference_progress_m_s'),
    act_progress=get('actual_progress_m_s'), f_ext=get('external_force_base_n'),
    qp=np.asarray([x['qp_intervention'] for x in rows], dtype=bool),
    saturated=np.asarray([x['saturated'] for x in rows], dtype=bool),
)
np.savez_compressed(dst, **out)
print(dst, len(rows), head['preparation'], sha[:12], flush=True)
