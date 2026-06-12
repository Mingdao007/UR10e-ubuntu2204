# Step4e Line Program Review Note

Generated on 2026-06-09 for the first straight-line paper-style outer-loop test after Step4d.

## TP Open Programs

- Preview, no motion: `/programs/andyl/kunwei/step4/step4e_preview_line_v1.urp`
- Contact hold: `/programs/andyl/kunwei/step4/step4e_contact_hold_line_v1.urp`
- Full line: `/programs/andyl/kunwei/step4/step4e_line_outerloop_v1.urp`

Each package has `.urp`, `.txt`, and `.script` uploaded to the same controller folder.

## Path

The line is defined by two Teach Pendant Move screenshots:

- Start/lower TCP pose: `[0.43301, 0.10802, -0.39179, 3.133, 0.529, 0.191]`
- End/higher TCP pose: `[0.49274, 0.23877, -0.38146, 2.968, 0.717, 0.007]`
- XY unit vector: `[0.415512246, 0.909587417]`
- XY length: `0.143751504 m`

Only the XY projection is used for the path. Z is not interpolated; normal motion is controlled by the force outer loop after contact latch.

## Control Boundary

- URScript owns the scaffold and consumes Cartesian `speedl` twist commands.
- Ubuntu bridge computes Step4e outer-loop commands from Kunwei zeroed wrench and RTDE actual TCP pose.
- IK remains inside the UR controller through Cartesian `speedl`.
- This is a medium-fidelity reproduction of the paper direction, not a strict joint-space finite-time RNN implementation.

## Register Map

- Existing force/safety inputs: input double registers `24..36`
- Step4e command inputs: input double registers `37..47`
- Command twist:
  - `37`: vx m/s
  - `38`: vy m/s
  - `39`: vz m/s
  - `40`: wx rad/s
  - `41`: wy rad/s
  - `42`: wz rad/s, expected zero
  - `43`: command valid
  - `44`: path progress m
  - `45`: force error N
  - `46`: orientation error rad
  - `47`: controller state

## Safety Defaults

- Search: deterministic downward `speedl` at `3 mm/s`, acceleration `300 mm/s^2`, no force admittance before contact latch
- Contact latch: normal force `<= -1 N` or force norm `> 1.5 N`
- Target force: `5 N`
- Raw normal guard: `20 N`
- Force norm guard: `50 N`
- Torque norm guard: `0.6 Nm`
- Sensor stale limit: `100 ms`
- Recoverable stop: retract `10 mm`, then return to the TP start pose if the robot can still move

## Local Entry Points

- `scripts/step4e-preview-v1-autowatch.sh`
- `scripts/step4e-hold-v1-autowatch.sh`
- `scripts/step4e-line-v1-autowatch.sh`

Autowatch waits for the exact expected TP program to be running before starting Kunwei streaming or writing RTDE input registers.

## V2 Audit Response

Step4e v2 keeps v1 intact and adds an entry-pose re-zero gate before contact search:

- Preview, no motion: `/programs/andyl/kunwei/step4/step4e_preview_line_v2.urp`
- Contact hold with entry re-zero: `/programs/andyl/kunwei/step4/step4e_contact_hold_line_v2.urp`
- Full line with entry re-zero: `/programs/andyl/kunwei/step4/step4e_line_outerloop_v2.urp`
- Local autowatch:
  - `scripts/step4e-preview-v2-autowatch.sh`
  - `scripts/step4e-hold-v2-autowatch.sh`
  - `scripts/step4e-line-v2-autowatch.sh`

V2 writes `output_double_register_34 = 0.0` at program start, moves to the reference
orientation and entry XY, then writes `output_double_register_34 = 1.0` at stage `23.0`.
The bridge re-baselines for `--rezero-s 1`; URScript waits for `sensor_ok` to drop and
return. If that does not complete, stop reason `14.0` is emitted and the program uses the
recoverable retract/home path.

This resolves Claude's v1 baseline-at-wrong-orientation concern, but it does not prove the
force-control sign. The required run order remains: v2 preview -> v2 hold -> inspect hold
CSV for `baseline_epoch=1`, `zero_events`, near-zero pre-contact force, and force convergence
toward 5 N -> only then v2 line.

---

# >>> CLAUDE AUDIT (2026-06-09) — READ THIS BEFORE RUNNING `line` <<<

Static review by Claude (Opus 4.8). No bridge started, no robot moved.
Full report: `STEP4E_LINE_AUDIT_CLAUDE.md` (same folder). Summary below.

Verdict: architecture + safety envelope are sound. **Cleared to run `preview` then `hold`.
Do NOT run `line` until the two hardware assumptions below are confirmed in the `hold` run.**

## MUST validate on hardware before `line` (hardware-dependent, not code bugs)

1. **Baseline is zeroed at the start pose, but the robot reorients before contact.**
   Bridge takes the zero (`--baseline-s 5`) at the stationary start pose; URScript then does
   `movel` to the fixed reference orientation `[3.133,0.529,0.191]` and to the entry XY
   (`step4e_line_outerloop_v1.script:151,156`) before searching/contacting. A force sensor's
   gravity/payload projection is orientation-dependent, so the zero captured at one
   orientation carries an offset at another. This biases the contact latch (`force_norm>1.5`),
   the 5 N target, and the computed contact normal `n_reaction_b`.
   → In the `hold` CSV, confirm `fz_n_zeroed`/`force_norm_n` ≈ 0 *after* reorientation and
   *before* contact. If not, re-zero at the entry pose, or accept the offset only if it is
   small vs 1.5 N (per the v1 idealized-assumptions agreement).
   Note: the bridge CAN re-zero (re-baselines when `output_double_register_34` rises,
   `kunwei_rtde_bridge.py:894-909`), but NO URScript writes register 34, so today there is no
   in-program re-zero and `--rezero-s 1` is dead.

2. **Force-control sign convention is unverified end to end.**
   `kunwei_to_tcp_wrench` negates Fy/Fz (`kunwei_rtde_bridge.py:235`), plus
   `--normal-axis fz --normal-sign 1`, plus `force_cmd = -n_reaction_b*normal_velocity`
   (`:325`). If any sign is wrong, the normal loop pushes AWAY from the surface → force
   diverges to the 20 N guard or contact is lost. Highest-consequence unverified assumption.
   → The `hold` run is the test: confirm force CONVERGES to +5 N (not diverges) and commanded
   `vz` points into the surface. Only then run `line`.

The preview→hold→line ladder exists for exactly these two. The operator script enforces
program identity, but hold-before-line is operator discipline — **read the hold CSV before
line.**

## Important (non-blocking, fix when convenient)

3. **`--duration-s 180` is a hard self-terminate independent of the robot program**
   (`kunwei_rtde_bridge.py:772`). If the full program exceeds 180 s, the bridge drops RTDE
   mid-line → URScript staleness → auto-home. Safe but an avoidable abort; nominal ~80 s but a
   slow search+line can approach it. Suggest raising to ~240 s.
4. **URScript `t`/`t2` are loop-iteration budgets, not wall-clock seconds** (each loop runs
   `speedl(t=0.002)` + register reads, so real time > 2 ms/iter). `search_runtime_limit_s=25`
   and `line_runtime_limit_s=75` under-count; real terminators are depth (60 mm)/progress
   (0.1437 m) — fine for safety. For `hold`, `line_runtime_limit_s=12` is the SUCCESS
   criterion, so the actual hold runs somewhat longer than 12 s wall-clock.
5. **Dead re-zero path** (same as note in §1): wire a register-34 write into the URScript at
   the entry pose (also fixes §1), or drop the unused `--rezero-s`.

## Housekeeping

6. Commit `2aa45bd` is clean and pushed, but the working tree overall is NOT clean (unrelated
   modified `docs/`,`report/`,`ft_sensor/` files + untracked `controller_backups/` etc.).
   Stage selectively before any future commit so unrelated changes aren't swept in.

## Confirmed OK / positive
Clean control boundary (Python only streams Kunwei start + writes RTDE registers; no URScript
upload, no program start, no robot motion, no `zero_ftsensor`; IK stays in UR via `speedl`).
Defense-in-depth guards (bridge-side 20/50/0.6 → stop_request AND URScript re-checks same +
100 ms staleness + command-magnitude sanity reason 13). Bridge command caps (6 mm/s, 15 mrad/s)
make the URScript per-axis limits unreachable in normal use — good redundancy. Heartbeat
freshness gate before motion; bridge death → stop within 100 ms (≤0.6 mm drift). Path math
correct (unit vector, projection, progress clamp, path-error normal component removed so
tangential motion doesn't fight the force loop). Force loop is sane PI+damping with integral
windup clamp and no pre-contact windup. Loss-of-contact (<1 N) → cmd_valid=0 → reason 12
auto-home. Preview has no motion nodes; all three `.urp` decompress and embed the matching
version stamp.

## Recommended order
preview → hold (read CSV, confirm the 3 things in §2) → line. Optional code follow-ups: §3,§4,§5.

# >>> END CLAUDE AUDIT <<<

---

# >>> CLAUDE v13 AUDIT (2026-06-09) — READ THIS FOR THE LATEST RUN <<<

Audit of v13 (code + the real run `bridge_step4e_line_outerloop_v13_autowatch_20260609_141027`).
Full report: `STEP4E_V13_AUDIT_CLAUDE.md` (same folder). No code changed, no robot moved.

Verdict: **v13 is a good program-flow + path-completion baseline with UNACCEPTABLE force
regulation.** Line completes (UR stop reason 1), XY tracking excellent (0.05 mm mean), but the
normal loop overshoots to **−36 N vs the 5 N target** (stage25 MAE 8.54 N, p95 22.3 N).
**Do NOT re-run v13 as-is** — the raw normal guard is at 100 N, too little margin over 36 N.

## v1 audit items — now resolved
- #1 baseline-at-wrong-orientation: **RESOLVED** by the entry-pose re-zero. Proof: start-pose
  baseline carried `fz` offset 2.97 N and stage-20 norm peaked 9.86 N; after re-zero, free-space
  stage-24.0 norm is ~0.54 N. The v1 concern was real and is now fixed.
- #2 force sign: **RESOLVED empirically** (sign correct, it loads in; magnitude wrong).
- #3 duration 180 s: **RESOLVED** (240 s). #5 dead re-zero: **RESOLVED** (wired).
- #4 loop-budget timers: unchanged (C3). #6 dirty tree: still dirty (H1).

## New v13 findings
- **F1 (HIGH, root cause):** the bridge controller integrates during stage 24.2 (~3.4 s of
  near-search/contact) before URScript consumes its output at stage 25 — `control_allowed` is
  true once force≥1 N and `reset_line_contact()` only fires for stage<24 or ≥26
  (`kunwei_rtde_bridge.py:284-287,302-392`). So line control starts PRE-SATURATED and slams in.
  Fix: gate integrator/normal-velocity on stage 25, or reset at the 24→25 transition.
- **F2 (HIGH, root cause):** velocity-command admittance (±3 mm/s clamp, tiny Kp, slow integral)
  vs a stiff surface → saturation/windup overshoot. Lower normal-velocity limit (~0.5–1 mm/s),
  cut Ki / tighten integral clamp, add a settle stage. Target p95 |error| < 3–5 N.
- **S1 (SAFETY HIGH):** raw normal guard = 100 N in both bridge and URScript
  (`step4e_line_outerloop_v13.script:76`). Lower to 30–50 N before any rerun; confirm KWR75B
  overload spec.
- **S2 (SAFETY MED):** 15 mm/s far-search trusts the hardcoded 80 mm free-space margin +
  `fixed_search_start_z=0.09835`. If the workpiece height drifts, contact can land in the fast
  phase. Verify surface-height repeatability.
- **C1:** internal servo ~250 Hz (bridge/RTDE 500 Hz); don't claim 500 Hz closed loop. (Author
  already documents this.)
- **C2:** no `preview_line_v13` exists — `preview-v13-autowatch` would fail. **C3:** timers are
  loop-iteration budgets, not seconds.

## Confirmed OK
Boundary intact (Python only writes registers). Entry re-zero works. Latched-normal + 0.95/0.05
blend is a real improvement. Two-stage + fixed search-start z fixes v12's no-contact miss. XY
tracking 0.05 mm mean / 0.31 mm max. Endpoint success + retract/home work; safety stayed NORMAL.
`.urp` verified to embed the v13 stamp + 0.09835.

## Next steps for codex
1. Lower guard to 30–50 N (S1) before rerun. 2. Fix F1 (clean stage-25 start). 3. Fix F2
(softer normal loop + settle). 4. Keep XY speed / search-start z / latched-normal / endpoint
logic. 5. Optional: add preview_v13, clean the tree.

# >>> END CLAUDE v13 AUDIT <<<
