# Step4e Line Outer-Loop — Claude Audit Report

Date: 2026-06-09
Auditor: Claude (Opus 4.8)
Scope: static review of the Step4e straight-line "outer loop / velocity loop" reproduction
before any robot motion. No bridge started, no robot moved.

Files reviewed:
- `programs/step4e_line_outerloop_v1.script` (+ `.urp`, `.txt`)
- `programs/step4e_contact_hold_line_v1.script` (+ `.urp`)
- `programs/step4e_preview_line_v1.script` (+ `.urp`)
- `tools/kunwei_rtde_bridge.py`
- `scripts/step4e-line-v1-operator.sh` + the three `~/ur10e_ros2_ws/scripts/*-autowatch.sh`
- `STEP4E_LINE_REVIEW.md`

Verdict: **architecture and safety envelope are sound; cleared to run `preview` then `hold`.**
Do **not** run `line` until the two hardware-dependent assumptions in §1 are confirmed
during the `hold` run.

---

## 1. Must validate on hardware before `line` (hardware-dependent, not code bugs)

### 1.1 Force-sensor baseline is taken at the start pose, then the robot reorients before contact
- The bridge captures the zero/baseline (`--baseline-s 5`) while the robot is stationary at
  the TP start pose, *before* any motion.
- The URScript then does `movel(reference_orientation_pose)` and `movel(entry_xy_pose)`
  (`step4e_line_outerloop_v1.script:151,156`) — i.e. it reorients to the fixed reference
  orientation `[3.133, 0.529, 0.191]` and then searches/contacts.
- A force sensor's gravity/payload projection is **orientation-dependent**. The baseline is
  only valid at the orientation where it was captured. After the reorientation the "zeroed"
  wrench carries an offset equal to the change in tool-weight projection.
- This biases three things: the contact latch (`force_norm > 1.5`, `step4e_line_outerloop_v1.script:183`),
  the 5 N force target, and the computed contact-normal direction `n_reaction_b`.
- **Why it matters:** if the start orientation differs much from the reference orientation,
  or the tool is heavy, the offset can be comparable to the 1.5 N latch / 5 N target.
- **Action:** during the `hold` run, read `fz_n_zeroed` / `force_norm_n` from the bridge CSV
  *after* the reorientation and *before* contact — they should be ≈ 0. If they are not,
  either re-zero at the entry pose, or accept the known offset (per the v1 "idealized
  assumptions" agreement) only if it is small relative to 1.5 N.
- Note: the re-zero plumbing exists in the bridge (it re-baselines when
  `output_double_register_34` rises, `kunwei_rtde_bridge.py:894-909`) **but no URScript ever
  writes register 34**, so today there is no in-program re-zero. `--rezero-s 1` is effectively
  dead. See §3.3.

### 1.2 Force-control sign convention is unverified end to end
- The Kunwei→TCP mapping negates Fy/Fz (`kunwei_to_tcp_wrench`, `kunwei_rtde_bridge.py:235`),
  `--normal-axis fz --normal-sign 1`, and the closed loop drives
  `force_cmd = -n_reaction_b * normal_velocity` (`kunwei_rtde_bridge.py:325`).
- If any of these signs is wrong, the normal loop pushes *away* from the surface instead of
  into it; force diverges from 5 N and the run either loses contact or climbs to the 20 N
  raw-normal guard.
- **Why it matters:** this is the single highest-consequence unverified assumption for a
  contact controller.
- **Action:** the `hold` run is exactly the test — confirm the measured contact force
  *converges to +5 N* (not diverges), and that the commanded `vz` points into the surface.
  Only proceed to `line` after `hold` converges cleanly.

These two are the reason the preview → hold → line ladder exists. Respect it: **run hold and
read its CSV before line.** The operator script enforces the program identity, but nothing
procedurally forces hold-before-line except operator discipline.

---

## 2. Correctness review — findings that are fine / positive

- **Control boundary is clean and well-enforced.** Python only streams the Kunwei start
  command (gated by `--allow-kunwei-stream-command`) and writes RTDE input registers (gated
  by `--write-rtde-inputs`). It never uploads URScript, starts a program, moves the robot,
  writes TCP/payload, or calls `zero_ftsensor`. IK stays in the UR controller via `speedl`.
  Documented at `kunwei_rtde_bridge.py:1-9,641-647`.
- **Defense in depth on guards.** Bridge side trips `stop_request` on 20 N / 50 N / 0.6 Nm
  (`guard_stop_reason`, `kunwei_rtde_bridge.py:562-569`); URScript independently re-checks the
  same raw limits plus a 100 ms staleness guard plus command-magnitude sanity (reason 13)
  (`step4e_line_outerloop_v1.script:44-62,233-244`).
- **Command-magnitude guards are conservative and correct.** Bridge caps total linear at
  0.006 m/s (norm) and angular at 0.015 rad/s; URScript rejects any axis > 0.010 m/s or
  > 0.030 rad/s and any wz > 0.005. The bridge caps make those URScript limits unreachable in
  normal operation — good redundancy.
- **Heartbeat freshness gate before any motion** (`codex_wait_for_fresh_heartbeat`,
  requires register 26 to change *and* sensor_ok register 27 > 0.5), and per-iteration
  staleness checks during search and line. If the bridge dies, registers freeze, heartbeat
  stops, URScript stops within 100 ms and auto-homes. At 6 mm/s that is ≤ 0.6 mm of drift.
- **Path math is correct.** Unit vector, scalar projection for `progress`, clamp to
  `[0, length]`, and removal of the path-error component along the contact normal so the
  tangential command does not fight the force loop (`kunwei_rtde_bridge.py:289-306`).
- **Force loop is a sane PI + damping admittance** with integral windup clamp (±10),
  normal-velocity clamp (3 mm/s), total-linear clamp (6 mm/s). Integral only accumulates once
  `force_abs ≥ 1 N`, so there is no pre-contact windup (`kunwei_rtde_bridge.py:308-330`).
- **Loss-of-contact is handled safely.** If force drops below 1 N during the line, the bridge
  sets `cmd_valid = 0`, URScript hits reason 12 and auto-homes
  (`step4e_line_outerloop_v1.script:239-240`).
- **Preview truly has no motion.** `step4e_preview_line_v1.script` contains no `movel`/`speedl`
  nodes (only echo + guard loop). The `.urp` files independently decompress and embed the
  matching version stamp `2026-06-09T1149HKT_STEP4E_*_V1`.
- **`hold` = `line` with tangent speed forced to 0** (`kunwei_rtde_bridge.py:295`), so it is a
  faithful, lower-risk subset of the line controller — a good gate.

---

## 3. Important — robustness / clarity (not blocking, fix when convenient)

### 3.1 Bridge `--duration-s 180` is a hard self-terminate independent of the robot program
- The bridge exits at 180 s regardless of robot state (`kunwei_rtde_bridge.py:772-774`). If the
  full program (baseline 5 s + approach + search up to ~25 s + line up to ~48–75 s) ever
  exceeds 180 s, the bridge closes RTDE mid-line → URScript staleness → reason 2 → auto-home.
- The failure mode is *safe* (auto-home), but it is an avoidable, surprising abort. Nominal
  total is ~80 s, but a slow search plus a slow line can approach the budget.
- **Suggestion:** raise `--duration-s` to comfortably exceed the worst-case program time
  (e.g. 240 s) or derive it from the expected program duration.

### 3.2 URScript phase timers are loop-iteration budgets, not wall-clock seconds
- `t`/`t2` increment by `hold_s` (2 ms) per loop iteration, but each iteration also runs
  `speedl(..., t=0.002)` plus register reads, so real elapsed time per iteration is > 2 ms.
  Therefore `search_runtime_limit_s = 25` and `line_runtime_limit_s = 75` are **not** 25 s/75 s
  of wall-clock; they under-count. The real terminators are depth (60 mm) and progress
  (0.143751504 m), which is fine for safety.
- For **hold mode this is user-visible**: `line_runtime_limit_s = 12` is the *success*
  criterion (`step4e_contact_hold_line_v1.script:245`), so the actual hold is somewhat longer
  than 12 s of wall-clock. Just be aware when reading hold timing.
- **Suggestion:** if a true wall-clock bound is wanted, drive the limits from a real clock;
  otherwise rename the variables to reflect they are iteration budgets.

### 3.3 Dead re-zero path
- As noted in §1.1, the bridge re-baseline trigger (`output_double_register_34`) is never
  written by any URScript, and `--rezero-s 1` is consequently unused. Either wire a re-zero
  write into the URScript at the entry pose (this would also resolve §1.1 cleanly), or drop
  the unused arg so it doesn't imply a capability that isn't active.

---

## 4. Minor / housekeeping

- The Step4e commit `2aa45bd` is clean and pushed, but the **working tree is not clean
  overall**: unrelated modified files (`docs/...`, `report/...`, `ft_sensor/...` reorg) and
  untracked dirs (`controller_backups/...`, `experiments/sensor-integration/kunwei-kwr75b/`, etc.) are present.
  Before any future commit, stage selectively so unrelated changes are not swept in.
- `report`/handoff describe search accel as 300 mm/s²; script uses `search_accel_m_s2 = 0.300`
  m/s² — consistent. (Cross-checked: 3 mm/s search, 5 N target, 20/50/0.6 guards all match
  between `STEP4E_LINE_REVIEW.md`, the operator script, and the bridge defaults.)

---

## 5. Recommended next steps (in order)

1. Run `preview` (no motion) and confirm the Step4e command registers populate and echo as
   expected, sensor_ok = 1, heartbeat advancing.
2. Run `hold`. **Before declaring it good, open the bridge CSV and confirm:**
   (a) `fz_n_zeroed`/`force_norm_n` ≈ 0 after reorientation and before contact (§1.1);
   (b) contact force converges to +5 N and does not diverge (§1.2);
   (c) commanded `vz` is into the surface during the hold.
3. Only then run `line`. Watch `step4e_progress_m` increase monotonically to ~0.1437 m and
   force hold near 5 N.
4. Optional code follow-ups: §3.1 duration margin, §3.3 re-zero wiring, §3.2 timer naming.
