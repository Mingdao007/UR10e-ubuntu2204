"""Structural observability and observation-model evidence for the shared normal observer.

Read-only postprocessing of the round-2 tick cache (/tmp/ynv2/*.npz, regenerated from the
retained receipts with report/yield-normal-v2/discussion/scripts/extract_ticks.py).  Development
data only; no closed-loop runs.  Outputs JSON to stdout.

Sections
  task        analytic Lissajous tangent-direction Gramian, lateral-dominant windows, speed minimum
  cells       per cell: gate fractions, actual-vs-reference speed, gated direction Gramian,
              information seconds per tangent axis, lateral-window gate behaviour, energy ratio
  surface     true-normal excursion from approach (x/y components, spectrum, rotation rate) and the
              first-order tracking bound for an observer with gated effective rate
  coplanarity coplanarity residual (true normal) binned by |a|/s across non-tangent cells, filtered vs raw
  tangent     decomposition of the tangent-cell residual into external-push and contact parts
  frozen_tilt analytic closed-loop cost of a frozen tilted prior (force axis, path offset, feedforward leak)
"""
import glob, json, os, sys
import numpy as np

W, A, B = 0.1, 0.04, 0.01
KP = 120.0
GATE_M_S = 0.002
EX = np.array([1.0, 0.0, 0.0]); EY = np.array([0.0, -1.0, 0.0])  # tangent axes for approach (0,0,-1)

def proj(n, x): return x - np.einsum('ij,ij->i', n, x)[:, None] * n
def rms(x): return float(np.sqrt(np.mean(np.asarray(x) ** 2))) if len(x) else None
def q(x, p): return float(np.quantile(x, p)) if len(x) else None

def task_section():
    t = np.arange(0.0, 2 * np.pi / W, 0.002)
    vx = A * W * np.cos(W * t); vy = 2 * B * W * np.cos(2 * W * t); sp = np.hypot(vx, vy)
    th = np.stack([vx, vy], 1) / sp[:, None]
    G = (th[:, :, None] * th[:, None, :]).mean(0)
    lat = np.abs(vy) > np.abs(vx)
    edges = t[np.flatnonzero(np.diff(lat.astype(int)))]
    bins = np.floor(t).astype(int)
    wy = np.array([(vy ** 2 / sp ** 2)[bins == k].mean() for k in range(int(t[-1]) + 1)])
    return {'gramian_xy': G.tolist(), 'eigenvalues': np.linalg.eigvalsh(G).tolist(),
            'speed_min_mm_s': 1e3 * float(sp.min()), 'speed_median_mm_s': 1e3 * float(np.median(sp)),
            'speed_max_mm_s': 1e3 * float(sp.max()), 'excitation_gate_mm_s': 1e3 * GATE_M_S,
            'lateral_dominant_fraction': float(lat.mean()), 'lateral_windows_s': edges.reshape(-1, 2).tolist(),
            'speed_in_lateral_windows_mm_s': [1e3 * float(sp[lat].min()), 1e3 * float(sp[lat].max())],
            'y_info_weight_per_second': np.round(wy, 3).tolist(),
            'y_info_seconds_ungated': float(wy.sum()), 'x_info_seconds_ungated': float((1 - wy).sum()),
            'note': 'reference kinematics only; |v|^2 = 16c^4 + 4 (mm/s)^2 with c = cos(0.1 t), minimum 2 mm/s at the lateral sweeps'}

def cell_section(path, lat_windows):
    d = np.load(path); t = d['t']; n = d['n_true']; v = d['v']; rv = d['ref_vel']; f = d['f_filt']
    vt = proj(n, v); s = np.linalg.norm(vt, axis=1); rvt = proj(n, rv); rs = np.linalg.norm(rvt, axis=1)
    a = np.einsum('ij,ij->i', n, v); sp = np.linalg.norm(v, axis=1)
    cg = d['contact_gate']; eg = d['excitation_gate']; g = cg & eg
    th = np.stack([vt @ EX, vt @ EY], 1); thn = th / np.maximum(np.linalg.norm(th, axis=1, keepdims=True), 1e-12)
    gram = lambda m: (thn[m, :, None] * thn[m, None, :]).mean(0).tolist()
    cop = np.einsum('ij,ij->i', n, np.cross(f, v)) / np.maximum(np.linalg.norm(f, axis=1) * sp, 1e-12)
    out = {'file': os.path.basename(path), 'sha256_12': str(d['sha256'])[:12], 'scenario': str(d['scenario']),
           'material': str(d['material']),
           'estimator': {k: json.loads(str(d['estimator_parameters']))[k] for k in ('motion_gain', 'force_correction_gain')},
           'contact_gate_frac': float(cg.mean()), 'excitation_gate_frac': float(eg.mean()), 'both_gates_frac': float(g.mean()),
           'actual_tangent_speed_median_mm_s': 1e3 * float(np.median(s)), 'reference_tangent_speed_median_mm_s': 1e3 * float(np.median(rs)),
           'direction_gramian_all': gram(t >= 0), 'direction_gramian_gated': gram(g),
           'x_info_seconds_gated': float(((thn[:, 0] ** 2) * g).sum() * 0.002),
           'y_info_seconds_gated': float(((thn[:, 1] ** 2) * g).sum() * 0.002),
           'windows': {}}
    wins = {'pre': t < 20, 'int': (t >= 20) & (t < 35.5), 'post': t >= 35.5}
    for i, (lo, hi) in enumerate(lat_windows): wins[f'lat{i + 1}'] = (t >= lo) & (t < hi)
    for name, m in wins.items():
        mg = m & g
        cos = np.einsum('ij,ij->i', vt[m], rvt[m]) / np.maximum(s[m] * rs[m], 1e-12)
        out['windows'][name] = {
            'energy_ratio_a2_over_s2': float((a[m] ** 2).mean() / max((s[m] ** 2).mean(), 1e-18)),
            'abs_a_over_s_median': float(np.median(np.abs(a[m]) / np.maximum(s[m], 1e-9))),
            'gate_open_frac': float(g[m].mean()), 'actual_s_mean_mm_s': 1e3 * float(s[m].mean()),
            'reference_s_mean_mm_s': 1e3 * float(rs[m].mean()), 'cos_actual_reference_median': float(np.median(cos)),
            'frac_s_ge_gate': float((s[m] >= GATE_M_S).mean()),
            'coplanarity_true_rms': rms(cop[mg]), 'coplanarity_true_p99': q(np.abs(cop[mg]), 0.99)}
    return out

def surface_section(path):
    d = np.load(path); n = d['n_true']; ap = d['approach']
    e = n - ap; cx = e @ EX; cy = e @ EY
    ang = np.degrees(np.arccos(np.clip(n @ ap, -1, 1)))
    rate = np.degrees(np.linalg.norm(np.diff(n, axis=0), axis=1) / 0.002)
    F = np.fft.rfft(cx - cx.mean()); fr = np.fft.rfftfreq(len(cx), 0.002); k = np.argsort(np.abs(F))[::-1][:3]
    bound = {}
    for gamma_eff in (0.05, 0.1, 0.17, 0.3, 1.0):
        # first-order tracking of a sinusoid at the task fundamental: residual = |1-H| * amplitude
        amp = float(np.degrees(2 * np.abs(F[k[0]]) / len(cx)))
        bound[str(gamma_eff)] = {'residual_amplitude_deg': amp * W / np.hypot(gamma_eff, W), 'tracking_gain': gamma_eff / np.hypot(gamma_eff, W)}
    return {'file': os.path.basename(path), 'prior_error_rms_deg': rms(ang), 'prior_error_max_deg': float(ang.max()),
            'x_component_rms_deg': float(np.degrees(rms(cx))), 'x_component_max_deg': float(np.degrees(np.abs(cx).max())),
            'y_component_rms_deg': float(np.degrees(rms(cy))), 'y_component_max_deg': float(np.degrees(np.abs(cy).max())),
            'rotation_rate_rms_deg_s': rms(rate), 'rotation_rate_max_deg_s': float(rate.max()),
            'dominant_freq_hz': fr[k].tolist(), 'dominant_amp_deg': np.degrees(2 * np.abs(F[k]) / len(cx)).tolist(),
            'first_order_tracking_bound_at_task_fundamental': bound,
            'note': 'x is the along axis (base +x); y the lateral axis (base -y); approach is (0,0,-1)'}

def coplanarity_section(paths):
    edges = [0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 10.0]
    acc = [([], []) for _ in edges[:-1]]
    for p in paths:
        d = np.load(p); n = d['n_true']; v = d['v']; f = d['f_filt']; fr = d['f_raw']
        a = np.einsum('ij,ij->i', n, v); s = np.linalg.norm(proj(n, v), axis=1); sp = np.linalg.norm(v, axis=1)
        g = d['contact_gate'] & d['excitation_gate']; r = np.abs(a) / np.maximum(s, 1e-9)
        cop = np.abs(np.einsum('ij,ij->i', n, np.cross(f, v))) / np.maximum(np.linalg.norm(f, axis=1) * sp, 1e-12)
        copr = np.abs(np.einsum('ij,ij->i', n, np.cross(fr, v))) / np.maximum(np.linalg.norm(fr, axis=1) * sp, 1e-12)
        for k in range(len(edges) - 1):
            m = g & (r >= edges[k]) & (r < edges[k + 1]); acc[k][0].extend(cop[m].tolist()); acc[k][1].extend(copr[m].tolist())
    rows = []
    for k in range(len(edges) - 1):
        c = np.array(acc[k][0]); cr = np.array(acc[k][1])
        rows.append({'abs_a_over_s_bin': [edges[k], edges[k + 1]], 'n_ticks': int(len(c)),
                     'filtered_rms': rms(c), 'filtered_p99': q(c, 0.99), 'filtered_max': float(c.max()) if len(c) else None,
                     'raw_rms': rms(cr), 'raw_p99': q(cr, 0.99)})
    return {'cells': [os.path.basename(p) for p in paths], 'gates': 'contact and excitation, true normal', 'bins': rows,
            'note': 'filtered = controller 20 ms force filter as seen by the estimator; raw = 4 ms sensor lag only'}

def tangent_section(path):
    d = np.load(path); t = d['t']; n = d['n_true']; v = d['v']; f = d['f_filt']; fe = d['f_ext']; rv = d['ref_vel']; ne = d['n_est']
    m = (t >= 20.5) & (t < 30.5) & d['contact_gate'] & d['excitation_gate']
    sp = np.linalg.norm(v, axis=1); vt = proj(n, v); s = np.linalg.norm(vt, axis=1); den = np.maximum(np.linalg.norm(f, axis=1) * sp, 1e-12)
    cop = np.einsum('ij,ij->i', n, np.cross(f, v)) / den; cop_ext = np.einsum('ij,ij->i', n, np.cross(fe, v)) / den
    cop_c = np.einsum('ij,ij->i', n, np.cross(f - fe, v)) / den
    ang_ext = np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i', fe, vt) / np.maximum(np.linalg.norm(fe, axis=1) * s, 1e-12), -1, 1)))
    frame = np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i', ne, n), -1, 1)))
    a = np.einsum('ij,ij->i', n, v)
    return {'file': os.path.basename(path), 'sha256_12': str(d['sha256'])[:12], 'window_s': [20.5, 30.5],
            'coplanarity_total_rms': rms(cop[m]), 'coplanarity_external_part_rms': rms(cop_ext[m]), 'coplanarity_contact_part_rms': rms(cop_c[m]),
            'equivalent_tilt_of_total_deg': float(np.degrees(np.arcsin(min(1.0, rms(cop[m]))))),
            'angle_external_push_vs_actual_tangent_velocity_deg_median': float(np.median(ang_ext[m])),
            'legacy_frame_error_deg_median': float(np.median(frame[m])), 'external_force_n_median': float(np.median(np.linalg.norm(fe[m], axis=1))),
            'energy_ratio_a2_over_s2': float((a[m] ** 2).mean() / (s[m] ** 2).mean()),
            'note': 'legacy frame; the push follows the reference tangent while friction follows the actual tangent velocity'}

def frozen_tilt_section(paths):
    out = []
    for p in paths:
        d = np.load(p); t = d['t']; n = d['n_true']; v = d['v']; f = d['f_filt']; load = d['load_true']
        m = (t < 20) & d['contact_gate'] & d['excitation_gate']
        mu_eff = float(np.median(np.linalg.norm(proj(n, f)[m], axis=1) / np.maximum(load[m], 1e-9)))
        rows = []
        for tilt_deg in (5.0, 8.53, 10.0, 15.0, 21.8):
            dl = np.radians(tilt_deg)
            for tilt_axis_name, e in (('x_error', EX), ('y_error', EY)):
                for slide_name, th in (('+x', EX), ('-x', -EX), ('+y', EY), ('-y', -EY)):
                    n_in = np.array([0.0, 0.0, -1.0]); n_out = -n_in
                    n_hat = n_in * np.cos(dl) + e * np.sin(dl)
                    # force axis: measured load along -n_hat is regulated to 5 N
                    denom = np.cos(dl) + mu_eff * float(th @ e) * np.sin(dl)
                    N = 5.0 / denom
                    fvec = N * n_out - mu_eff * N * th
                    pf = fvec - float(fvec @ n_hat) * n_hat
                    leak = 4.47e-3 * np.sin(dl) * float(th @ e)
                    rows.append({'tilt_deg': tilt_deg, 'tilt_axis': tilt_axis_name, 'sliding': slide_name,
                                 'true_load_n': N, 'path_offset_mm': 1e3 * float(np.linalg.norm(pf)) / KP,
                                 'feedforward_normal_leak_mm_s_at_max_speed': 1e3 * leak})
        base = 1e3 * mu_eff * 5.0 / KP
        out.append({'file': os.path.basename(p), 'material': str(d['material']), 'mu_eff_measured': mu_eff,
                    'untilted_friction_offset_mm_predicted': base, 'untilted_path_rms_mm_measured': 1e3 * rms(d['path_err']),
                    'normal_speed_cap_mm_s': 3.0, 'rows': rows})
    return {'model': 'n_hat = n cos d + e sin d; force loop holds f.(-n_hat)=5 N; path offset = |P_nhat f|/Kp; leak = (P_nhat v_ref).n_true',
            'cells': out, 'note': 'steady-state, small-error, ideal Coulomb contact; no ringing, no orientation coupling; predictions for the frozen tilted cells'}

if __name__ == '__main__':
    cache = sys.argv[1] if len(sys.argv) > 1 else '/tmp/ynv2/*.npz'
    files = sorted(glob.glob(cache))
    task = task_section()
    result = {'task': task,
              'cells': [cell_section(p, task['lateral_windows_s']) for p in files],
              'surface': [surface_section(p) for p in files if 'forceoff' in p and 'nominal' in p],
              'coplanarity_vs_normal_motion': coplanarity_section([p for p in files if 'tangent' not in p]),
              'tangent_cells': [tangent_section(p) for p in files if 'tangent' in p],
              'frozen_tilt_prediction': frozen_tilt_section([p for p in files if 'forceoff' in p and 'nominal' in p])}
    print(json.dumps(result, indent=1))
