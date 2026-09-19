"""Synthetic, evaluator-free demonstration of the motion-tangent rectification law.

Fixed true inward normal n = z.  Sliding at speed s along x, plus zero-mean
normal velocity a(t) = A sin(w t).  Force f = N n - mu N t_hat (Coulomb, N=5, mu=0.15).
Observers start with a 3 deg tilt either along the sliding direction (para) or
perpendicular to it (perp).  Reports the fitted exponential rate of the error
over 20 s for NV (NO-v2 arithmetic, gamma=1, no cap) and for CP and the gated
two-residual observer TR.  Prediction for NV: rate = gamma (A^2/2 - s^2)/(s^2 + A^2/2)
(para) and gamma (A^2/2)/(s^2 + A^2/2) (perp), before saturation.
"""
import json, sys
import numpy as np
DT = 0.002
def unit(a): return a / max(np.linalg.norm(a), 1e-15)
def step(kind, n, v, f, g1=1.0, g2=1.0, cap=None, eps=0.002, kappa=np.tan(np.radians(15)), rho=np.sin(np.radians(15)), gated=True, energy=None, beta=0.5, tau_e=2.0):
    P = np.eye(3) - np.outer(n, n); g = np.zeros(3); pv = P @ v
    if np.linalg.norm(pv) < 0.002: return n
    if energy is not None:  # slow EWMA energies of normal and tangential velocity in the ESTIMATED frame
        al = DT / tau_e; energy[0] += al * (np.dot(n, v) ** 2 - energy[0]); energy[1] += al * (pv @ pv - energy[1])
    if kind in ('NV', 'TR', 'TRu', 'TRs'):
        r1 = np.dot(n, v)
        ok = {'NV': True, 'TRu': True, 'TR': abs(r1) <= kappa * np.linalg.norm(pv), 'TRs': energy is None or energy[0] <= beta * energy[1]}[kind]
        if ok: g += g1 * r1 * pv / max(v @ v, eps ** 2)
    if kind in ('CP', 'TR', 'TRu', 'TRs'):
        c = np.cross(f, v); cn = np.linalg.norm(c)
        if cn > 1e-9:
            c /= cn; r2 = np.dot(n, c)
            if kind == 'CP' or abs(r2) <= rho:
                g += g2 * r2 * (P @ c)
    if cap is not None:
        r = np.linalg.norm(g)
        if r > cap: g *= cap / r
    return unit(n - DT * g)
def run(kind, tilt_dir, A, s=0.0045, w=2 * np.pi * 1.5, T=20.0, mu=0.15, N=5.0, gamma=1.0):
    z = np.array([0, 0, 1.]); x = np.array([1., 0, 0]); y = np.array([0, 1., 0])
    e = x if tilt_dir == 'para' else y
    n = unit(np.cos(np.radians(3)) * z + np.sin(np.radians(3)) * e)
    t = np.arange(0, T, DT); err = np.empty_like(t); energy = np.array([0.0, s * s]); nsum = np.zeros(3); cnt = 0
    for i, ti in enumerate(t):
        a = A * np.sin(w * ti); v = s * x + a * z
        vt = np.array([s, 0, 0.]); f = N * z - mu * N * vt / np.sqrt(vt @ vt + 0.002 ** 2)
        n = step(kind, n, v, f, g1=gamma, g2=gamma, energy=energy)
        err[i] = np.degrees(np.arccos(np.clip(np.dot(n, z), -1, 1)))
        if ti >= T - 2.0: nsum += n; cnt += 1
    m = (t >= 1) & (err > 0.05) & (err < 60)
    rate = float(np.polyfit(t[m], np.log(np.radians(err[m])), 1)[0]) if m.sum() > 100 else None
    nbar = unit(nsum / max(cnt, 1)); mean_err = float(np.degrees(np.arccos(np.clip(np.dot(nbar, z), -1, 1))))
    return {'kind': kind, 'tilt': tilt_dir, 'A_over_s': A / s, 'err0': float(err[0]), 'err_end': float(err[-1]), 'err_last2s_rms': float(np.sqrt(np.mean(err[t >= T - 2] ** 2))), 'err_of_mean_n_last2s': mean_err, 'fitted_rate_per_s': rate,
            'predicted_NV_rate': float((A * A / 2 - s * s) / (s * s + A * A / 2)) if tilt_dir == 'para' else float((A * A / 2) / (s * s + A * A / 2))}
out = []
for kind in ('NV', 'CP', 'TR', 'TRu', 'TRs'):
    for tilt in ('para', 'perp'):
        for ratio in (0.0, 0.3, 0.7, 1.0, 1.5):
            out.append(run(kind, tilt, ratio * 0.0045))
print(json.dumps(out, indent=1))
