"""Time-binned diagnostics (1 s bins of PATH time) from cached ticks.  Read-only."""
import json, sys, glob, os
import numpy as np
def unit(a): return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-15)
def ang(a, b): return np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i', a, b), -1, 1)))
def proj(n, x): return x - np.einsum('ij,ij->i', n, x)[:, None] * n
def binned(path):
    d = np.load(path); t = d['t']; n_true = d['n_true']; n_est = d['n_est']; v = d['v']; f = d['f_filt']
    approach = d['approach']; A = np.tile(approach, (len(t), 1)); load = d['load_true']
    i20 = np.searchsorted(t, 20.0); n20 = n_est[i20]
    e_est = ang(n_est, n_true); tilt20 = ang(n_est, np.tile(n20, (len(t), 1))); est_prior = ang(n_est, A)
    v_n = np.einsum('ij,ij->i', n_true, v); vt = proj(n_true, v); speed = np.linalg.norm(v, axis=1)
    disp = np.cumsum(v_n) * 0.002; disp -= disp[i20]
    c = np.cross(f, v); cop = np.einsum('ij,ij->i', n_true, c) / np.maximum(np.linalg.norm(f, axis=1) * speed, 1e-12)
    vr = proj(n_true, d['ref_vel']); phi = ang(unit(vt), unit(vr))
    fmag = np.linalg.norm(f, axis=1); cosd = np.cos(np.radians(e_est))
    rows = []
    for b in range(int(np.floor(t[-1])) + 1):
        m = (t >= b) & (t < b + 1)
        if not m.any(): continue
        rows.append({'t': b, 'err_est_deg': float(np.sqrt(np.mean(e_est[m] ** 2))), 'tilt_from_t20_deg': float(np.sqrt(np.mean(tilt20[m] ** 2))),
            'est_vs_prior_deg': float(np.mean(est_prior[m])), 'vn_rms_mm_s': float(1e3 * np.sqrt(np.mean(v_n[m] ** 2))),
            'vn_mean_mm_s': float(1e3 * np.mean(v_n[m])), 'net_disp_since_20_mm': float(1e3 * disp[m][-1]),
            'speed_mean_mm_s': float(1e3 * np.mean(speed[m])), 'ratio_median': float(np.median(np.abs(v_n[m]) / np.maximum(speed[m], 1e-9))),
            'cop_true_rms': float(np.sqrt(np.mean(cop[m] ** 2))), 'phi_v_vs_ref_median_deg': float(np.median(phi[m])),
            'load_mean_n': float(np.mean(load[m])), 'load_min_n': float(load[m].min()), 'fmag_mean_n': float(np.mean(fmag[m])),
            'five_cos_delta_n': float(np.mean(5 * cosd[m]))})
    return {'file': os.path.basename(path), 'rows': rows}
if __name__ == '__main__':
    files = sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else '/tmp/ynv2/*.npz'))
    print(json.dumps([binned(p) for p in files]))
