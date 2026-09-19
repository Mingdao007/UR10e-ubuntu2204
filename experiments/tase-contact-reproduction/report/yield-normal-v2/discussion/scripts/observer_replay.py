"""Open-loop observer replays on retained ticks (read-only postprocessing).

Feeds the recorded measured TCP velocity and the recorded filtered wrist force
into candidate observers and compares to evaluator truth.  This tests the
observation model only; it is NOT a closed-loop result (the trajectory was
produced under a different estimate, so estimate-dependent coupling is absent).

Observers
  NV  : NO-v2 normalized motion-tangent update, g = gamma (n.v) P_n v / max(|v|^2, eps^2)
  CP  : coplanarity update, c = f x v / |f x v|, g = gamma_c (n.c) P_n c
Both: contact gate (|f|>=1 N and -n.f>=1 N), excitation gate (|P_n v|>=2 mm/s),
rate cap 0.1 rad/s, unit-sphere renormalization, no force-direction pull.
Priors: approach, or approach tilted by +tilt_deg about a tangent axis.
"""
import json, sys, glob, os
import numpy as np

DT = 0.002
def unit(a): return a / max(np.linalg.norm(a), 1e-15)
def ang(a, b): return float(np.degrees(np.arccos(np.clip(np.dot(a, b), -1, 1))))
def rot(axis, deg):
    axis = unit(axis); th = np.radians(deg); K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K

def run(kind, n0, v, f, gamma, cap=0.1, eps=0.002, contact_n=1.0, excite=0.002):
    n = unit(n0).copy(); out = np.empty_like(v); applied = 0
    for i in range(len(v)):
        vi = v[i]; fi = f[i]
        P = np.eye(3) - np.outer(n, n)
        tangent_speed = np.linalg.norm(P @ vi)
        gate = (np.linalg.norm(fi) >= contact_n) and (np.dot(fi, -n) >= contact_n) and (tangent_speed >= excite)
        if gate:
            if kind == 'NV':
                g = gamma * np.dot(n, vi) * (P @ vi) / max(vi @ vi, eps ** 2)
            else:
                c = np.cross(fi, vi); cn = np.linalg.norm(c)
                if cn < 1e-9: g = np.zeros(3)
                else:
                    c = c / cn; g = gamma * np.dot(n, c) * (P @ c)
            r = np.linalg.norm(g)
            if r > cap: g = g * (cap / r)
            n = unit(n - DT * g); applied += 1
        out[i] = n
    return out, applied / len(v)

def evaluate(path, gammas_nv=(1.0,), gammas_cp=(0.5,), tilts=(0.0, 10.0)):
    d = np.load(path); t = d['t']; v = d['v']; f = d['f_filt']; n_true = d['n_true']; approach = d['approach']
    # tangent axis for tilting: true-tangent direction of the initial reference velocity
    P0 = np.eye(3) - np.outer(approach, approach); ax = unit(P0 @ d['ref_vel'][0])
    ax2 = unit(np.cross(approach, ax))
    res = {'file': os.path.basename(path), 'scenario': str(d['scenario']), 'material': str(d['material']),
           'prior_err_rms_deg': float(np.sqrt(np.mean([ang(approach, x) ** 2 for x in n_true[::10]]))),
           'closed_loop_err_rms_deg': float(np.degrees(np.sqrt(np.mean(d['n_err'] ** 2)))), 'runs': []}
    for tilt in tilts:
        for axis_name, axis in (('along', ax), ('lateral', ax2)):
            if tilt == 0.0 and axis_name == 'lateral': continue
            n0 = rot(axis, tilt) @ approach if tilt else approach
            for kind, gammas in (('NV', gammas_nv), ('CP', gammas_cp)):
                for gamma in gammas:
                    est, frac = run(kind, n0, v, f, gamma)
                    err = np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i', est, n_true), -1, 1)))
                    w = lambda m: float(np.sqrt(np.mean(err[m] ** 2)))
                    below = np.where(err < 2.0)[0]
                    res['runs'].append({'kind': kind, 'gamma': gamma, 'tilt_deg': tilt, 'tilt_axis': axis_name,
                        'update_frac': frac, 'err0_deg': float(err[0]),
                        'rms_pre_deg': w(t < 20), 'rms_int_deg': w((t >= 20) & (t < 35.5)), 'rms_post_deg': w(t >= 35.5),
                        'rms_all_deg': w(t >= 0), 'max_deg': float(err.max()), 'final_deg': float(err[-1]),
                        'rms_last10s_deg': w(t >= t[-1] - 10),
                        't_first_below_2deg_s': float(t[below[0]]) if len(below) else None,
                        'drift_from_prior_deg': ang(est[-1], n0)})
    return res

if __name__ == '__main__':
    files = sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else '/tmp/ynv2/*.npz'))
    print(json.dumps([evaluate(p) for p in files], indent=1))
