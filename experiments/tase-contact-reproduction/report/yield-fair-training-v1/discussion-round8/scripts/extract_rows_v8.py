"""Round-8 read-only extractor: one listed training artifact -> compact npz.

Reads ONLY artifacts listed in report/yield-fair-training-v1/round8-input-snapshot.json,
verifies the file sha256 against the snapshot evidence before parsing, and writes
/tmp/yfp8/<attempt_id>.npz plus <attempt_id>.meta.json. No artifact is modified.
Rows are on the PATH clock; records aligned to rows are the records whose
reference phase is 'path' (entry records precede them).
"""
import hashlib, json, sys, time
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[4]
SNAP = ROOT / 'report/yield-fair-training-v1/round8-input-snapshot.json'
OUT = Path('/tmp/yfp8')
ROW_SCALARS = ('time_s', 'true_normal_load_n', 'force_error_n', 'estimated_force_error_n', 'path_error_m',
               'orientation_error_rad', 'normal_estimation_error_rad', 'actual_progress_m_s',
               'reference_progress_m_s', 'task_scale')
ROW_BOOLS = ('saturated', 'qp_intervention')
ROW_VEC3 = ('position_m', 'reference_position_m', 'external_force_base_n', 'normal_estimate', 'true_inward_normal')
RES_SCALARS = ('commanded_tangent_progress_m_s', 'tangent_saturation_m_s', 'measured_progress_m_s',
               'planned_progress_m_s', 'normal_speed_m_s', 'force_error_n', 'sample_time_s', 'observation_age_s')
RES_BOOLS = ('normal_saturated', 'qp_intervention', 'memory_reset', 'path_clock_frozen')
RES_VEC3 = ('law_force_base_n', 'law_velocity_base_m_s', 'law_state', 'law_normal_velocity_m_s',
            'law_tangent_velocity_m_s', 'filtered_force_base_n', 'integral_n_s', 'force_residual_base_n',
            'inward_normal_base', 'offset_base_m', 'reference_velocity_base_m_s')
EST_BOOLS = ('contact_gate', 'excitation_gate', 'motion_update_applied', 'coplanarity_update_applied')
EST_SCALARS = ('tangent_speed_m_s', 'signed_normal_load_n')


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def extract(entry):
    aid = entry['id']; path = Path(entry['artifact_path'])
    expected = entry['evidence']['artifact_sha256']
    t0 = time.time()
    digest = sha256_file(path)
    if digest != expected:
        raise SystemExit(f'{aid}: sha256 mismatch {digest} != {expected}')
    art = json.loads(path.read_text())
    rows = art.pop('rows'); records = art.pop('records')
    out = {}
    for k in ROW_SCALARS: out['row_' + k] = np.asarray([r[k] for r in rows], dtype=float)
    for k in ROW_BOOLS: out['row_' + k] = np.asarray([bool(r[k]) for r in rows], dtype=bool)
    for k in ROW_VEC3: out['row_' + k] = np.asarray([r[k] for r in rows], dtype=float)
    out['row_rotation'] = np.asarray([r['rotation'] for r in rows], dtype=float)
    path_records = [rec for rec in records if rec['reference'].get('phase') == 'path']
    entry_records = [rec for rec in records if rec['reference'].get('phase') != 'path']
    if len(path_records) != len(rows):
        raise SystemExit(f'{aid}: path records {len(path_records)} != rows {len(rows)}')
    out['rec_path_time_s'] = np.asarray([rec['reference']['path_time_s'] for rec in path_records], dtype=float)
    out['rec_obs_time_s'] = np.asarray([rec['observation']['time_s'] for rec in path_records], dtype=float)
    out['rec_law22'] = np.asarray([rec['controller_snapshot']['law22']['values'] for rec in path_records], dtype=float)
    out['entry_law22'] = np.asarray([rec['controller_snapshot']['law22']['values'] for rec in entry_records], dtype=float)
    out['rec_reference_force_n'] = np.asarray([rec['reference']['force_n'] for rec in path_records], dtype=float)
    out['rec_obs_linear_velocity'] = np.asarray([rec['observation']['linear_velocity_base_m_s'] for rec in path_records], dtype=float)
    out['rec_obs_raw_force'] = np.asarray([rec['observation']['raw_force_base_n'] for rec in path_records], dtype=float)
    out['rec_obs_injection'] = np.asarray([rec['observation']['software_injection_base_n'] for rec in path_records], dtype=float)
    res = [rec['result'] for rec in path_records]
    for k in RES_SCALARS: out['res_' + k] = np.asarray([r.get(k, np.nan) for r in res], dtype=float)
    for k in RES_BOOLS: out['res_' + k] = np.asarray([bool(r.get(k, False)) for r in res], dtype=bool)
    for k in RES_VEC3: out['res_' + k] = np.asarray([r.get(k, [np.nan] * 3) for r in res], dtype=float)
    est = [r.get('estimator', {}) for r in res]
    for k in EST_BOOLS: out['est_' + k] = np.asarray([bool(e.get(k, False)) for e in est], dtype=bool)
    for k in EST_SCALARS: out['est_' + k] = np.asarray([e.get(k, np.nan) for e in est], dtype=float)
    out['est_inward_normal_base'] = np.asarray([e.get('inward_normal_base', [np.nan] * 3) for e in est], dtype=float)
    np.savez_compressed(OUT / f'{aid}.npz', **out)
    meta = {k: art[k] for k in art if k not in ('final_controller_snapshot', 'final_simulator_snapshot',
                                                    'formal_initial_snapshot', 'initial_controller_snapshot',
                                                    'initial_simulator_snapshot')}
    meta['final_controller_snapshot_law22'] = art['final_controller_snapshot']['law22']
    meta['formal_initial_controller_law22'] = art['formal_initial_snapshot']['controller']['law22']
    meta['_extract'] = {'artifact_path': str(path), 'artifact_sha256': digest, 'n_rows': len(rows),
                        'n_records': len(records), 'n_entry_records': len(entry_records),
                        'seconds': round(time.time() - t0, 1)}
    (OUT / f'{aid}.meta.json').write_text(json.dumps(meta, indent=1, sort_keys=True))
    print(f'{aid}: ok rows={len(rows)} records={len(records)} entry={len(entry_records)} {time.time()-t0:.1f}s', flush=True)


def main(argv):
    snap = json.loads(SNAP.read_text())
    wanted = set(argv[1:])
    OUT.mkdir(parents=True, exist_ok=True)
    for entry in snap['attempts']:
        if wanted and entry['id'] not in wanted: continue
        if (OUT / f"{entry['id']}.npz").exists() and (OUT / f"{entry['id']}.meta.json").exists():
            print(f"{entry['id']}: cached", flush=True); continue
        extract(entry)


if __name__ == '__main__':
    main(sys.argv)
