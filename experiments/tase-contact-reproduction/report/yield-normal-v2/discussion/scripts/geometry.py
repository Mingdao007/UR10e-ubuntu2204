"""Per-cell geometry diagnostics from cached ticks (read-only postprocessing).

Windows are PATH time: pre [0,20), intervention [20,35.5), post [35.5,end].
Outputs JSON to stdout.  Angles in degrees, forces in N, speeds in mm/s.
"""
import json, sys, glob, os
import numpy as np

KP = 120.0
def unit(a): return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-15)
def ang(a, b): return np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i', a, b), -1, 1)))
def rms(x): return float(np.sqrt(np.mean(np.asarray(x) ** 2))) if len(x) else None
def proj(n, x): return x - np.einsum('ij,ij->i', n, x)[:, None] * n

def analyse(path):
    d = np.load(path)
    t = d['t']; n_true = d['n_true']; n_est = d['n_est']; approach = d['approach']
    v = d['v']; f = d['f_filt']; f_raw = d['f_raw']; load = d['load_true']
    A = np.tile(approach, (len(t), 1))
    fdir = unit(-f); in_contact = np.linalg.norm(f, axis=1) >= 1.0
    e_est = ang(n_est, n_true); e_prior = ang(A, n_true); est_prior = ang(n_est, A)
    est_force = ang(n_est, fdir); true_force = ang(n_true, fdir)
    v_n = np.einsum('ij,ij->i', n_true, v); v_t = np.linalg.norm(proj(n_true, v), axis=1)
    speed = np.linalg.norm(v, axis=1); ratio = np.abs(v_n) / np.maximum(speed, 1e-9)
    c = np.cross(f, v); cn = np.linalg.norm(c, axis=1)
    cop_true = np.einsum('ij,ij->i', n_true, c) / np.maximum(np.linalg.norm(f, axis=1) * speed, 1e-12)
    c_raw = np.cross(f_raw, v)
    cop_raw = np.einsum('ij,ij->i', n_true, c_raw) / np.maximum(np.linalg.norm(f_raw, axis=1) * speed, 1e-12)
    cop_est = np.einsum('ij,ij->i', n_est, c) / np.maximum(np.linalg.norm(f, axis=1) * speed, 1e-12)
    # friction/restoring identity in the estimated frame
    ft_est = np.linalg.norm(proj(n_est, f), axis=1)
    et_est = np.linalg.norm(proj(n_est, d['path_err_base']), axis=1)
    ft_true = np.linalg.norm(proj(n_true, f), axis=1)
    windows = {'pre': (t >= 0) & (t < 20), 'int': (t >= 20) & (t < 35.5), 'post': t >= 35.5, 'all': t >= 0}
    out = {'file': os.path.basename(path), 'sha256': str(d['sha256'])[:12], 'scenario': str(d['scenario']),
           'material': str(d['material']), 'estimator_parameters': json.loads(str(d['estimator_parameters'])),
           'gates': {'motion_applied_frac': float(d['motion_applied'].mean()),
                     'force_applied_frac': float(d['force_applied'].mean()),
                     'excitation_frac': float(d['excitation_gate'].mean()),
                     'contact_frac': float(d['contact_gate'].mean())}}
    for name, m in windows.items():
        mc = m & in_contact
        out[name] = {
            'n_ticks': int(m.sum()),
            'err_est_rms_deg': rms(e_est[m]), 'err_est_max_deg': float(e_est[m].max()),
            'err_prior_rms_deg': rms(e_prior[m]), 'err_prior_max_deg': float(e_prior[m].max()),
            'est_vs_prior_rms_deg': rms(est_prior[m]), 'est_vs_prior_max_deg': float(est_prior[m].max()),
            'est_vs_forcedir_rms_deg': rms(est_force[mc]), 'true_vs_forcedir_rms_deg': rms(true_force[mc]),
            'true_vs_forcedir_median_deg': float(np.median(true_force[mc])) if mc.any() else None,
            'vn_rms_mm_s': 1e3 * rms(v_n[m]), 'vt_rms_mm_s': 1e3 * rms(v_t[m]),
            'vn_over_speed_median': float(np.median(ratio[m])), 'vn_over_speed_p90': float(np.quantile(ratio[m], .9)),
            'frac_ratio_gt_0p3': float((ratio[m] > 0.3).mean()),
            'coplanarity_true_filt_rms': rms(cop_true[mc]), 'coplanarity_true_filt_p99': float(np.quantile(np.abs(cop_true[mc]), .99)) if mc.any() else None,
            'coplanarity_true_raw_rms': rms(cop_raw[mc]),
            'coplanarity_est_rms': rms(cop_est[mc]),
            'tangent_force_est_frame_rms_n': rms(ft_est[m]), 'tangent_force_true_frame_rms_n': rms(ft_true[m]),
            'path_err_est_frame_rms_mm': 1e3 * rms(et_est[m]),
            'identity_ft_over_kp_minus_et_rms_mm': 1e3 * rms(ft_est[m] / KP - et_est[m]),
            'path_err_true_frame_rms_mm': 1e3 * rms(d['path_err'][m]),
            'load_min_n': float(load[m].min()), 'load_max_n': float(load[m].max()),
            'contact_loss_ticks': int((load[m] < 1.0).sum()),
        }
    return out

if __name__ == '__main__':
    files = sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else '/tmp/ynv2/*.npz'))
    print(json.dumps([analyse(p) for p in files], indent=1))
