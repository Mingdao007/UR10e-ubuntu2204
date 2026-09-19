"""Time-resolved open-loop observer replay on cached ticks (read-only postprocessing).

For each cell the recorded measured TCP velocity and the recorded controller-filtered force are fed
to candidate observers started from the approach prior or from a prior tilted so that the error is
along the task x axis (along) or the task y axis (lateral).  The error is decomposed per 1 s bin into
its x and y components.  This tests the observation model and the task's excitation only; the
recorded trajectory was produced under a different estimate, so estimate-dependent closed-loop
coupling (force axis, path projection, feedforward leak, orientation) is absent.

Observers (gamma1 = gamma2 = 0.3 /s unless named, cap 0.05 rad/s, eps 2 mm/s, contact and excitation
gates as in the estimator, no residual or cone gates):
  NV   g = g1 (n.v) P v / max(|v|^2, eps^2)
  NV1  same with g1 = 1.0 /s
  CP   g = g2 (n.c) P c,  c = f x v / |f x v|
  NVCP sum of NV and CP
Usage: tilt_replay_binned.py '<cache glob>'  -> JSON on stdout
"""
import glob, json, os, sys
from multiprocessing import Pool
import numpy as np

DT = 0.002; EPS = 0.002; CAP = 0.05; GATE_N = 1.0; GATE_V = 0.002
EX = np.array([1.0, 0.0, 0.0]); EY = np.array([0.0, -1.0, 0.0])
def unit(a): return a / max(np.linalg.norm(a), 1e-15)
def rot(axis, deg):
    axis = unit(axis); th = np.radians(deg)
    K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K

def run(kind, n0, v, f, g1, g2):
    n = unit(n0).copy(); out = np.empty_like(v); gate = np.zeros(len(v), bool)
    for i in range(len(v)):
        vi = v[i]; fi = f[i]; P = np.eye(3) - np.outer(n, n); pv = P @ vi
        if not (np.linalg.norm(fi) >= GATE_N and np.dot(fi, -n) >= GATE_N and np.linalg.norm(pv) >= GATE_V):
            out[i] = n; continue
        gate[i] = True; g = np.zeros(3)
        if kind in ('NV', 'NV1', 'NVCP'):
            g += g1 * np.dot(n, vi) * pv / max(vi @ vi, EPS ** 2)
        if kind in ('CP', 'NVCP'):
            c = np.cross(fi, vi); cn = np.linalg.norm(c)
            if cn > 1e-9:
                c = c / cn; g += g2 * np.dot(n, c) * (P @ c)
        r = np.linalg.norm(g)
        if r > CAP: g = g * (CAP / r)
        n = unit(n - DT * g); out[i] = n
    return out, gate

def job(args):
    path, kind, tilt_deg, tilt_axis = args
    d = np.load(path); t = d['t']; v = d['v']; f = d['f_filt']; n_true = d['n_true']; approach = d['approach']
    # error along x: rotate approach about the y axis; error along y: rotate about the x axis
    n0 = approach if tilt_deg == 0 else (rot(EY, -tilt_deg) @ approach if tilt_axis == 'x' else rot(EX, tilt_deg) @ approach)
    e0 = n0 - approach
    if tilt_deg and abs(float(e0 @ (EX if tilt_axis == 'x' else EY))) < 0.9 * np.sin(np.radians(tilt_deg)):
        n0 = rot(EY, tilt_deg) @ approach if tilt_axis == 'x' else rot(EX, -tilt_deg) @ approach
    g1 = 1.0 if kind == 'NV1' else 0.3
    est, gate = run(kind, n0, v, f, g1, 0.3)
    e = est - n_true; ex = np.degrees(e @ EX); ey = np.degrees(e @ EY)
    err = np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i', est, n_true), -1, 1)))
    bins = np.floor(t).astype(int); nb = bins.max() + 1
    b = lambda x, fn=np.mean: [float(fn(x[bins == k])) for k in range(nb)]
    below = np.where(err < 2.0)[0]
    return {'file': os.path.basename(path), 'observer': kind, 'tilt_deg': tilt_deg, 'tilt_axis': tilt_axis,
            'initial_error_deg': float(err[0]), 'initial_x_deg': float(ex[0]), 'initial_y_deg': float(ey[0]),
            'final_deg': float(err[-1]), 'rms_all_deg': float(np.sqrt(np.mean(err ** 2))), 'rms_last10s_deg': float(np.sqrt(np.mean(err[t >= t[-1] - 10] ** 2))),
            't_below_2deg_s': float(t[below[0]]) if len(below) else None, 'gate_frac': float(gate.mean()),
            'err_x_deg_1s': b(ex), 'err_y_deg_1s': b(ey), 'err_deg_1s': b(err), 'gate_frac_1s': b(gate.astype(float))}

if __name__ == '__main__':
    files = sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else '/tmp/ynv2/*.npz'))
    jobs = []
    for p in files:
        name = os.path.basename(p)
        if 'forceoff' in name:
            jobs += [(p, k, 0.0, 'none') for k in ('NV', 'CP', 'NVCP')]
            jobs += [(p, k, 10.0, ax) for ax in ('x', 'y') for k in ('NV', 'NV1', 'CP', 'NVCP')]
            if 'compliant' in name: jobs += [(p, k, 21.8, ax) for ax in ('x', 'y') for k in ('NV', 'CP', 'NVCP')]
        elif 'legacy' in name and 'tangent' in name:
            jobs += [(p, k, 0.0, 'none') for k in ('NV', 'CP', 'NVCP')]
        elif 'legacy' in name and 'nominal' in name:
            jobs += [(p, k, 10.0, ax) for ax in ('x', 'y') for k in ('NV', 'CP', 'NVCP')]
    with Pool(min(30, len(jobs))) as pool: results = pool.map(job, jobs)
    print(json.dumps({'parameters': {'gamma1': 0.3, 'gamma1_NV1': 1.0, 'gamma2': 0.3, 'cap_rad_s': CAP, 'eps_m_s': EPS,
                                     'contact_gate_n': GATE_N, 'excitation_gate_m_s': GATE_V, 'residual_gate': None, 'cone_gate': None},
                      'runs': results}, indent=1))
