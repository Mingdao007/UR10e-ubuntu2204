# Step4e v13 — Claude Audit Report

Date: 2026-06-09
Auditor: Claude (Opus 4.8)
Scope: full audit of Step4e v13 (code **and** the real robot run). Read-only; no code changed,
no robot moved, no commit.

Artifacts reviewed:
- `programs/step4e_line_outerloop_v13.script` / `.urp` (+ hold v13)
- `tools/kunwei_rtde_bridge.py` (current; latched-normal controller)
- `scripts/step4e-line-v1-operator.sh` (now version-parameterized), `scripts/step4e-line-v13-autowatch.sh`
- run `runs/bridge_step4e_line_outerloop_v13_autowatch_20260609_141027/` (`metadata.json`,
  `summary.json`)
- `report/step4e-v13-fixed-search-start.md` + `report/assets/step4e-v13-fixed-search-start/metrics.json`

This complements the earlier static audit `STEP4E_LINE_AUDIT_CLAUDE.md` (v1). It closes out
the v1 items and adds v13-specific findings.

---

## Verdict
v13 is a **successful program-flow + path-completion baseline with unacceptable force
regulation.** The line completes (UR stop reason 1), XY tracking is excellent (0.05 mm mean),
but the normal-force loop overshoots to **−36.1 N against a 5 N target** (stage25 MAE 8.54 N,
abs-error p95 22.3 N, fz_mean −9.75 N). The author's own report documents this honestly.

**Do not re-run v13 as-is.** The raw normal guard is currently 100 N — only ~3× the observed
36 N overshoot, which is too little margin for a contact task.

---

## Status of the v1 audit items

| v1 item | Status in v13 | Evidence |
|---|---|---|
| #1 baseline zeroed at start pose, robot reorients before contact | **RESOLVED** | Entry-pose software re-zero added (write `output_double_register_34=1` at stage 23, bridge re-baselines, `codex_wait_for_rezero_complete`). `summary.json baseline_epoch=1`. See "concrete proof" below. |
| #2 force-control sign unverified | **RESOLVED (empirically)** | `--step4e-normal-command-sign -1` loads *into* the surface — it did, to −36 N. Sign correct; magnitude wrong. |
| #3 bridge `--duration-s 180` too short | **RESOLVED** | run used `duration_s=240`. |
| #5 dead re-zero path | **RESOLVED** | register-34 path wired and exercised. |
| #4 loop-budget timers (not wall-clock) | **UNCHANGED** | see C3. |
| #6 dirty working tree | **STILL DIRTY** | see H1. |

**Concrete proof #1 was real and is now fixed:** `metrics.json` shows the start-pose baseline
carried `baseline_si_offsets/fz_n = 2.97 N`, and stage-20 (wait, pre-re-zero) force norm peaked
at **9.86 N**. After the entry re-zero, free-space stage-24.0 force norm reads **~0.54 N**. That
~3–10 N orientation/gravity offset is exactly the v1 concern, and the v13 re-zero removed it.

---

## New findings (v13)

### F1 — HIGH (root cause of overshoot): the controller winds up during stage 24.2, before its output is consumed
In `compute_step4e_values` (`tools/kunwei_rtde_bridge.py:262-397`), `control_allowed` becomes
true as soon as `force_abs ≥ 1 N` and `normal_acquired` — which happens during the **near-search
/ contact stage 24.2** (≈3.36 s in this run). `reset_line_contact()` only fires for
`robot_stage < 24.0` or `≥ 26.0` (`:284-287`), so from stage 24.0 onward the integrator and
`normal_velocity_m_s` evolve freely. But URScript **ignores registers 37–42 until stage 25** —
during 24.x it runs the fixed-search `speedl` (`step4e_line_outerloop_v13.script:253-263`).

Net effect: while the bridge command is being thrown away during 24.2, the loop state keeps
integrating, so it enters line control (stage 25) **pre-saturated** — integrator and normal
velocity already at the inward clamp — and slams in at stage-25 start. This is a concrete,
fixable contributor on top of the general "too aggressive" the report notes.

**Fix:** gate integrator / `normal_velocity_m_s` evolution on `line_stage_active` (stage 25
only), or call `reset_line_contact()` at the 24→25 transition so line control starts from zero.

### F2 — HIGH (root cause): velocity-command admittance vs. a stiff surface overshoots
The normal loop integrates `accel_like = Kp·e + Ki·∫e − damping·v` into a velocity clamped at
±3 mm/s (`:364-392`). Against a stiff contact, holding ~3 mm/s inward for even ~0.1 s is a large
penetration → tens of N. The small `Kp = 7e-4` and `damping = 0.35` decelerate too slowly to
arrest the inward velocity once force exceeds target, and `Ki = 8e-5` against a ±10 N·s integral
clamp adds a persistent inward bias. That is the classic saturation/windup overshoot, seen as
−36 N.

**Fix:** lower `--step4e-normal-velocity-limit-m-s` (≈ 0.5–1 mm/s), reduce or zero `Ki` / tighten
the integral clamp, and add a force-settle stage before tangent motion. Target abs-error
p95 < 3–5 N before touching XY speed or the frequency architecture. (Report agrees.)

### S1 — SAFETY HIGH: raw normal guard is at 100 N
Both layers currently allow 100 N: bridge `max_normal_force_n=100` (`metadata.json`) and
URScript `codex_step4e_guard_stop_reason` `normal_force > 100`
(`step4e_line_outerloop_v13.script:76`). With the controller known to reach ~36 N, a slightly
stiffer contact point or a faster approach could climb much higher before the guard trips,
risking KWR75B overload, tool, or workpiece damage.

**Action:** lower to 30–50 N before any further run, and confirm against the KWR75B
rated/overload spec (not found in-repo — confirm from the vendor datasheet). The torque guard
was also raised 0.6 → 1.0 Nm; the run peaked at 0.564 Nm, so that margin is OK.

### S2 — SAFETY MED: 15 mm/s far-search depends on a hardcoded free-space margin
Two-stage search descends at **15 mm/s** (5× v1's 3 mm/s) for the first 80 mm of depth from the
fixed search-start `z = 0.09835 m`, then 3 mm/s for the final 12 mm
(`step4e_line_outerloop_v13.script:154-160,253-260`). This assumes the workpiece cannot be
reached during the 15 mm/s phase. The 1.5 N contact latch is checked every loop so a stray hit
still latches — but deceleration from 15 mm/s leaves more penetration than at 3 mm/s, and the
robot would then jump straight into line control from a fast-approach contact.

**Action:** verify surface-height repeatability run-to-run. The safety of the fast phase rests
entirely on `fixed_search_start_z` + the 80 mm margin being correct for the current setup.

### C1 — Fidelity caveat (accurately documented by the author): ~250 Hz internal servo, not 500 Hz
`summary.json`: bridge write 503.4 Hz, RTDE output 500.0 Hz, 0 reconnects — but the URScript
heartbeat-echo cadence is ~236 Hz overall (~249 Hz in stage 25). Each URScript loop is ≈ 4 ms
(`speedl(t=0.002)` + several `read_input_float_register` + `get_actual_tcp_pose` + echo writes).
So bridge/RTDE/logging are 500 Hz class, but the actual command-update rate is ~250 Hz. Do not
claim a 500 Hz closed loop. (The report already states this.)

### C2 — Minor: no `preview_line_v13`
Only `hold` and `line` v13 packages exist. `preview-v13-autowatch` would fail the loaded-program
check in the operator script. Either generate a v13 preview package or drop the v13 preview
entry point.

### C3 — Minor (unchanged from v1): loop-budget timers
`t` / `t2` / `end_hold_s` increment by `hold_s` (2 ms) per ~4 ms loop, so
`search_runtime_limit_s = 25`, `line_runtime_limit_s = 75`, and `end_hold_required_s = 0.1` are
iteration budgets, not wall-clock. Real protection is depth (92 mm) / progress, which is fine —
just don't read those limits as seconds.

---

## Confirmed OK / positive
- **Control boundary intact.** Python only streams the Kunwei start command and writes RTDE
  input registers; `metadata.json safety_boundary` holds (no URScript upload, no motion from
  Python, no TCP/payload writes, no `zero_ftsensor`, no Kunwei zero/tare). The robot moved, but
  all motion came from URScript consuming registers — not from Python.
- **Entry re-zero works** and removes the v1 orientation-bias offset (see proof above).
- **Latched contact normal** with a 0.95/0.05 low-pass blend (`:303-312`) is a real improvement
  over v1's per-sample normal — a steadier control direction.
- **Two-stage + fixed search-start z** fixes v12's height-dependent no-contact miss; contact was
  found cleanly in stage 24.2 (force norm 6.0 N) and the program entered line control.
- **XY path tracking excellent:** mean 0.050 mm, p95 0.137 mm, max 0.313 mm. The limitation is
  force, not geometry or UR IK.
- **Endpoint logic works:** `end_hold` over a progress threshold → stop reason 1 →
  unload/retract/home, safety stayed NORMAL throughout.
- **Delivery verified:** the `.urp` independently decompresses with the v13 stamp and `0.09835`;
  the author SHA-verified controller upload and cache read-back.

---

## Recommended next steps (for codex)
1. **Before any rerun:** lower the raw normal guard to 30–50 N (S1) and confirm the KWR75B
   overload spec.
2. **Fix F1:** reset/gate controller state to stage 25 so line control starts from a clean zero.
3. **Fix F2:** lower the normal-velocity limit, cut `Ki` / tighten the integral clamp, add a
   force-settle stage; aim for abs-error p95 < 3–5 N before changing speed or frequency.
4. **Keep** XY speed, fixed search-start z, latched-normal blend, and endpoint logic — they work.
5. Optional: generate `preview_line_v13` (C2); clean the working tree before the next commit (H1).

### H1 — Housekeeping
Working tree is dirty: unrelated modified `docs/`, `report/`, `ft_sensor/`, `weekly_meeting/`
files plus untracked `controller_backups/`, `ft_sensor/kunwei/kwr75b/`, etc. The v13 commits
(`09c49ff`, `ba50d6a`) are clean and pushed, but stage selectively before the next commit so
unrelated changes aren't swept in.

---

## Numbers cross-checked against run data
From `summary.json` / `metrics.json` (so the report's figures are independently confirmed):
stage25 fz_min −36.12 N, fz_mean −9.75 N, MAE 8.54 N, abs-error p95 22.34 N, force_norm_max
36.26 N, torque_norm_max 0.564 Nm; bridge 503.4 Hz, RTDE 500.0 Hz, echo ~236 Hz (stage25 ~249 Hz),
0 reconnects, baseline_epoch 1; start-pose baseline fz offset 2.97 N, stage-20 norm peak 9.86 N,
post-re-zero stage-24.0 norm ~0.54 N.
