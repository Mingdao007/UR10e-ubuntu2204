"""Open-loop replay of the gated two-residual observer TR on cached ticks.

TR: g = g1 r1 P v / max(|v|^2, eps^2) [only if |r1| <= kappa |P v|]
      + g2 r2 P c                     [only if |r2| <= rho],  r1 = n.v, r2 = n.c, c = f x v/|f x v|
Gates: contact (|f|>=1 N and -n.f>=1 N), excitation (|P v|>=2 mm/s), rate cap.
Also runs NV-only and CP-only with the same gains for comparison.  Not closed loop.
"""
import json, sys, glob, os
import numpy as np
DT = 0.002
def unit(a): return a / max(np.linalg.norm(a), 1e-15)
def rot(axis, deg):
    axis = unit(axis); th = np.radians(deg); K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K
def run(kind, n0, v, f, g1, g2, cap, kappa, rho, eps=0.002, beta=0.5, tau_e=2.0):
    n = unit(n0).copy(); out = np.empty_like(v); used1 = used2 = 0; energy = np.array([0.0, 1e-5])
    for i in range(len(v)):
        vi = v[i]; fi = f[i]; P = np.eye(3) - np.outer(n, n); pv = P @ vi; pvn = np.linalg.norm(pv)
        if not (np.linalg.norm(fi) >= 1.0 and np.dot(fi, -n) >= 1.0 and pvn >= 0.002):
            out[i] = n; continue
        al = DT / tau_e; energy[0] += al * (np.dot(n, vi) ** 2 - energy[0]); energy[1] += al * (pvn * pvn - energy[1])
        g = np.zeros(3)
        if kind in ('NV', 'TR', 'TRu', 'TRs'):
            r1 = np.dot(n, vi)
            ok = {'NV': True, 'TRu': True, 'TR': abs(r1) <= kappa * pvn, 'TRs': energy[0] <= beta * energy[1]}[kind]
            if ok: g += g1 * r1 * pv / max(vi @ vi, eps ** 2); used1 += 1
        if kind in ('CP', 'TR', 'TRu', 'TRs'):
            c = np.cross(fi, vi); cn = np.linalg.norm(c)
            if cn > 1e-9:
                c = c / cn; r2 = np.dot(n, c)
                if kind == 'CP' or abs(r2) <= rho:
                    g += g2 * r2 * (P @ c); used2 += 1
        r = np.linalg.norm(g)
        if r > cap: g = g * (cap / r)
        n = unit(n - DT * g); out[i] = n
    return out, used1 / len(v), used2 / len(v)
def evaluate(path, gamma=0.3, cap=0.05, kappa=np.tan(np.radians(15)), rho=np.sin(np.radians(15))):
    d = np.load(path); t = d['t']; v = d['v']; f = d['f_filt']; n_true = d['n_true']; approach = d['approach']
    P0 = np.eye(3) - np.outer(approach, approach); ax = unit(P0 @ d['ref_vel'][0]); ax2 = unit(np.cross(approach, ax))
    res = {'file': os.path.basename(path), 'gamma': gamma, 'cap': cap, 'kappa': kappa, 'rho': rho, 'runs': []}
    for tilt, axis_name, axis in ((0.0, 'none', ax), (10.0, 'toward_lateral', ax), (10.0, 'toward_along', ax2)):
        n0 = rot(axis, tilt) @ approach if tilt else approach
        for kind in ('NV', 'CP', 'TR', 'TRu', 'TRs'):
            est, u1, u2 = run(kind, n0, v, f, gamma, gamma, cap, kappa, rho)
            err = np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i', est, n_true), -1, 1)))
            w = lambda m: float(np.sqrt(np.mean(err[m] ** 2)))
            below = np.where(err < 2.0)[0]
            res['runs'].append({'kind': kind, 'tilt_deg': tilt, 'tilt': axis_name, 'nv_used_frac': u1, 'cp_used_frac': u2,
                'rms_pre': w(t < 20), 'rms_int': w((t >= 20) & (t < 35.5)), 'rms_post': w(t >= 35.5), 'rms_all': w(t >= 0),
                'max': float(err.max()), 'final': float(err[-1]), 't_below_2deg': float(t[below[0]]) if len(below) else None})
    return res
if __name__ == '__main__':
    print(json.dumps(evaluate(sys.argv[1]), indent=1))
