# Step5d Holistic Root-Cause Audit (v1–v4)

Date: 2026-06-15

Scope: not the single v4 stop reason. This audits the **whole Step5d
live-prep series** (v1, v2, v3, v4) as one problem, because each run stopped
for a *different* reason and the team has been fixing the reason instead of the
controller.

Verdict: **The Step5d outer loop has a normal-direction / sign-convention bug.
It has been broken since v2. v3 and v4 changed TP entry gates, not the
controller. The four "different stop reasons" are downstream symptoms of one
un-verified controller, run live four times without the no-contact sign
verification the 2026-06-10 TASE audit already made mandatory.**

---

## 核心结论（中文速览）

- 每次停的原因不一样，但**根因是同一个**：Step5d 外环把"期望法向 `R_d.z`"
  设成了 `+反作用力方向 u = +F/‖F‖`（指向**离开**接触面），而工具的接近轴
  `R_cur.z` 指向**进入**接触面。两者几乎反向（`dot = −0.999`），所以即使工具
  实际几乎垂直贴着表面，外环也算出**姿态误差 ≈178°**，并下发约 `5 rad/s` 的
  乱转指令把工具从面上撬开。
- **同一个 run、同一个姿态、被流水线自己测了两次、差了 ~180°**：接触搜索阶段
  （25.05/25.15/25.3）报 `orientation_error = 0.0374 rad ≈ 2.1°`（所以才跳过
  25.1/25.2 抬升与纠姿），一进 25.0 交给 Step5d 外环就跳成 `0.9999`（≈178°）。
  `2.1° + 178° ≈ 180°`。这就是符号反了的铁证。
- v2 力冲到 `−65 N`（撞进面）、v4 力掉到 `0 N`（飘离面）是**同一个发散控制器**
  在不同初始预载下的两个方向，不是两个独立问题。
- 这与 2026-06-10 TASE 审计钉死的 v20 根因（`wy_sign` 反号 + `wz` 丢弃）
  **同一类**（法向/姿态符号），但**不同代码路径**。那次审计要求"接触实验前先做
  P0 无接触符号验证 + 离线回放"。Step5d 这条新外环**从没跑过这个 P0**——
  `STEP5D_RNN_MAPPING_REVIEW.md` §七是用"沿用 Step5/Step6 约定"的**断言**关掉了
  这个 open question，而不是用经验回放验证。所以一个符号反了的 `R_d` 直接进了
  四次实物接触。

---

## 1. The pattern the user pointed at: different stop reason every time

| ver | entered 25.0? | stop reason | force signature in 25.0 | what was "fixed" next |
|---|---|---|---|---|
| v1 | yes, ~0.106 s | TP stop `2` (stale heartbeat) | n/a | warm Pinocchio/RNN runtime **before** 25.0 |
| v2 | yes, ~0.086 s | `normal_force_guard` (~−65 N) | normal load **18.2 → 65.4 N** (runs **into** surface) | add `xdot_c` limiter + 2–15 N entry window |
| v3 | **no** | 25.3 force-settle gate too narrow | n/a | widen 25.3 to a contact window |
| v4 | yes, ~0.158 s | TP stop `12` (cmd_valid lost) | normal load **6.06 → 0.00 N** (drifts **off** surface) | (this audit) |

Reading this table the way the user asked:

- **v1 is a genuinely separate, legitimate infra fix** (runtime init was on the
  hot path). Do not fold it into the controller story.
- **v2 is the first controller-failure evidence.** The instant the controller
  executed, force ran away to 65 N. That was the moment to stop and diagnose the
  controller.
- **v3 and v4 did not touch the controller.** They tuned the *entry gate*
  (whether/when you reach the controller). So the team had controller-failure
  evidence at v2 and spent two iterations on gates. That is exactly the
  "different stop reason each time" the user is describing.
- **v2 (into surface, +65 N) and v4 (off surface, 0 N) are opposite directions
  of the same divergence.** A correct force/orientation controller converges to
  the 5 N setpoint from either side. This one leaves the setpoint in whichever
  direction the entry condition seeds it. Opposite symptoms = one
  un-converging controller, not two bugs.

---

## 2. Root cause (proven on the run, by the pipeline itself)

### 2.1 The self-contradiction: 2.1° vs 178° on the same pose

`step4e_orientation_error_rad` in `bridge_step5d_strict_rnn_liveprep_v4_20260614_234951`:

| stage | who computes orientation_error | value |
|---|---|---|
| 25.05 latch | contact-search path (tool-z vs latched normal) | `0.0374 rad` (**2.1°**) |
| 25.15 skip decision | contact-search path | `0.0374 rad` (**2.1°**) |
| 25.3 contact window | contact-search path | `0.0374 rad` (**2.1°**) |
| **25.0 line control** | **Step5d outer loop `‖e_o‖`** | **`0.9999` (≈178°)** |

Same physical tool pose, no large motion in between. The contact-search
convention says the tool is **2.1° off normal** (which is *why* the v3/v4
4° orientation-skip fired and 25.1/25.2 ran 0 rows). The Step5d outer loop says
the tool is **178° off** its desired orientation. `2.1° + 178° ≈ 180°`. The two
halves of the same pipeline use **opposite normal sign**.

### 2.2 The line of code

`tools/step5d_paper_outer_loop.py`, `compute_step5d_outer_loop`:

```python
force_base = R_cur @ force_tcp                 # reaction force, base frame
u_force_base, ... = normalize_vector(force_base, (0,0,1))   # u = +F/||F||  (OUTWARD)
...
R_d = rotation_matrix_from_z_axis(u_force_base)            # sets R_d.z = u  (OUTWARD)
...
Q_d  = rotation_matrix_to_quaternion(R_d)
Q_cur= rotation_matrix_to_quaternion(R_cur)
e_qua, e_o = quaternion_orientation_error(Q_d, Q_cur)
xdot_o = ko * e_o                                          # ko = 5
```

In contact, the surface **reaction** force on the tool points *out of* the
surface, i.e. anti-parallel to the tool's *approach* axis `R_cur.z`. Setting
`R_d.z = +u` therefore asks the tool to flip ~180°.

### 2.3 Recompute of a real Stage-25.0 row (v4, row 0, clean 6 N contact)

```
force_t (TCP)      = [ 0.199, -0.185, -6.053]   |F| = 6.059 N
u (force_base dir) = [-0.0983,-0.0601,+0.9933]   (outward / +base-z)
R_cur.z (tool z)   = [ 0.1280, 0.0932,-0.9874]   (inward / -base-z)
dot(R_d.z, R_cur.z)= -0.9990                      <-- ANTI-PARALLEL
||e_o||            = 0.9999  (e_qua_w = -0.016 -> ~178.2 deg)
xdot_o = ko*||e_o||= 4.999  rad/s
```

This is **not** the "arbitrary roll" failure mode (that would give a partial
error). `dot(R_d.z, R_cur.z) = −0.999` is a clean **z-axis sign flip**: the
desired tool axis is the outward reaction normal; it should be the inward
contact/approach normal. Same class as the v20 `wy_sign` finding.

### 2.4 Why this dominates everything (the ~5.0 raw command)

`_step5d_outer_xdot_norm` is pinned at ≈`5.0` in **both** v2 and v4. From the
recompute, that entire ~5.0 is `xdot_o = ko·e_o` with `‖e_o‖ ≈ 1.0`. The
force/motion linear part is negligible by comparison. So the 6-D command the RNN
is asked to track is **almost pure bogus rotation** toward a 180°-wrong
orientation.

| run | `_step5d_outer_xdot_norm` (25.0) | `orientation_error` (25.0) |
|---|---|---|
| v2 | 5.0000 → 5.0001 (pinned) | 1.0000 (pinned) |
| v4 | 4.9994 → 4.9998 (pinned) | 0.9999 → 0.1415 |

### 2.5 Why the limiter does not save it

`limit_step5d_live_xdot` caps linear and angular **independently** and
**scales, never zeros**:

```python
if angular_norm > max_angular:
    limited[3:] *= max_angular / angular_norm   # direction preserved
```

So the bogus ~5 rad/s rotation is rate-capped to the angular limit and **still
executed**, about a ~180°-wrong axis. Through the Jacobian + TCP lever arm a
sustained capped rotation walks the contact point off the surface (v4: TCP z
+0.57 mm, force 6 → 0 N, ~0.03 rad/s angular). The limiter caps *speed*, not
*correctness*; tuning it (a v2→v4 theme) cannot fix direction.

### 2.6 Why force cannot recover once it drifts (D1 + D2)

Two more defects in the same function compound the orientation bug:

- **D1 — force loop is direction-blind.**
  `force_along_normal_n = dot(force_base, u_force_base)`. Because
  `u = force_base/‖force_base‖`, this is **identically `+‖force_base‖ ≥ 0`**.
  So `e_f = force_target − ‖F‖ = 5 − ‖F‖` and the restoring acceleration
  `xddot_p ∝ e_f · u` is applied along the live force direction. Force too high
  → push deeper (v2: 18 → 65 N). Force too low → push further off (v4 once
  contact thinned). This is an admittance loop that **diverges from 5 N in
  whichever direction it starts** — the force half of the "opposite symptoms."
  (Likely porting cause: paper `f_d = −5 N`; this code uses `force_target = +5`
  with `u = +F/‖F‖`. The setpoint sign was flipped without flipping the normal —
  verify against the paper convention before fixing.)

- **D2 — no latched normal.** The bridge passes the **live zeroed** force
  (`force_t`, line 778) into the outer loop, which recomputes `u` every tick.
  The v31 latched/hold-on-low-force normal (`_step4e_filtered_normal_b_*`) is
  computed but **never used** by the outer loop. So when force drops, `u`
  becomes normalized sensor noise. v4 Stage-25.0 row 40 (`|F| = 0.099 N`) shows
  `u = [−0.912, −0.033, −0.408]` — pure garbage direction. There is nothing to
  push back along.

### 2.7 Latent third defect (will bite after the sign fix)

`rotation_matrix_from_z_axis` builds `R_d`'s x/y axes by Gram–Schmidt from a
fixed reference, so the **roll about the normal is arbitrary** and unrelated to
the tool's actual roll. This is *not* the paper's Eq.(10) `R_d`. With the z-sign
fixed, `‖e_o‖` will drop from ~1.0 but will **not** go to ~0; a residual roll
error will remain and re-inject a spurious `xdot_o`. Fix `R_d` to the paper form
(minimal rotation aligning the approach axis to the inward normal) at the same
time, or the orientation command stays wrong, just less violently.

---

## 3. Causal chain (one diagram for the whole series)

```
normal defined as +reaction (outward)            [2.2]
        |                                          + arbitrary R_d roll [2.7]
        v
R_d.z anti-parallel to tool approach axis  ->  e_o ~= 178 deg          [2.1/2.3]
        v
xdot_o = ko*e_o ~= 5 rad/s dominates xdot_c (force/motion negligible)  [2.4]
        v
limiter rate-caps angular but preserves the bogus rotation             [2.5]
        v
RNN tracks it -> J + lever arm rotate the TCP off (or into) the face
        |                                                   |
        v (low preload: v4)                                 v (high preload: v2)
contact 6 -> 0 N, cmd_valid lost, TP stop 12        force 18 -> 65 N, force guard
        \_________________________  +  ___________________________/
                                    |
   force loop is direction-blind (e_f = 5-||F||) and uses unlatched live
   normal (noise at low force), so it never pulls back to 5 N            [2.6]
```

v1 (heartbeat) and v3 (entry gate too narrow) sit *upstream* of this chain —
they only changed whether/when the run reaches the broken controller.

---

## 4. Process finding: the mandated P0 was skipped for this controller

The 2026-06-10 TASE full audit
(`ur10e_lab_vault/handoffs/claude_fable5_ur10e_tase_full_audit_report_20260610.md`)
nailed the v20 root cause as `wy_sign = −1` flipping the base-frame correction
axis + `wz` discarded, in `kunwei_rtde_bridge.py`. Its **P0** requirement:
sign correction **+ offline replay verification + no-contact isolation test**,
before any attitude/contact experiment.

- This Step5d outer loop is the **same sign class** (normal/orientation
  direction) but a **different code path**: Step5d runs the joint `speedj` branch
  (`kunwei_rtde_bridge.py:1432–1457`, outer loop at 1366). The old `wy_sign` /
  `cross3` code is in the `else` branch (~line 1467) and is **not** on the
  Step5d path — so "we fixed wy_sign in June" does **not** cover this.
- **No Step5d no-contact sign-verification run exists.** The runs go straight
  from offline structural sanity (which uses a *synthetic* unit normal and
  *zeroed* position error — `STEP5_FLOW.md` lines 249–254, so it structurally
  cannot see D1/D2/D3) to live contact v1→v2→v3→v4.
- `STEP5D_RNN_MAPPING_REVIEW.md` §七 marked "Force sign convention" **closed**
  by *asserting* the Step5/Step6 bench convention
  (`normal_sign=1.0`, `step4e_normal_command_sign=1.0`,
  `target_force_n=5.0`). Asserting a convention is **not** the empirical
  no-contact replay the audit required. That is the exact hole a
  sign-inverted `R_d` walked through, four times.

---

## 5. What to actually do (controller first, gates last)

Priority order — do **not** loosen another TP gate before this:

1. **Fix the normal sign in the outer loop.** `R_d.z` must align with the
   **inward** contact normal (the tool approach direction / the latched normal
   the contact-search already uses and that the 2.1° metric validated), i.e. use
   `−u` for the orientation target, or define the control normal as the inward
   normal up front. Reconcile `force_target` sign with the paper `f_d = −5`
   convention at the same time so D1's `e_f` regains a direction.
2. **Build `R_d` from the paper Eq.(10) minimal rotation**, not
   `rotation_matrix_from_z_axis` Gram–Schmidt, to kill the latent arbitrary-roll
   error [2.7].
3. **Feed the latched/held normal into the outer loop**, not the raw live
   `force_t` [2.6 D2], so the control normal survives low force.
4. **Make the force error direction-aware** [2.6 D1]:
   `e_f = f_d − (F_b · n_latched)` with a signed projection on the fixed latched
   normal, not `5 − ‖F‖`.
5. **Re-run the audit-mandated P0 *before* any contact run:**
   - offline replay of the v4 (and v2) Stage-25.0 CSV rows through the fixed
     outer loop; assert `dot(R_d.z, R_cur.z) > 0`, `‖e_o‖` small (consistent
     with the 2.1° contact-search metric, not 178°), and `‖xdot_c‖` physical
     (not pinned at 5.0);
   - a **no-contact** sign isolation run (push by hand / free space) that proves
     the commanded normal and orientation point the right way with the sensor
     live — the step the series never did.
6. **Add a pipeline-consistency assertion**: at 25.0 entry, the outer-loop
   `‖e_o‖`-based orientation error must agree with the contact-search
   orientation error to within a tolerance. A ~180° jump at the 25.15→25.0
   boundary must hard-fail the controller, not be limited and executed.

Only after 1–6 pass should entry gates / limiter / qdot cap be revisited.

---

## 6. Evidence index (all primary, re-runnable)

- Outer-loop code: `tools/step5d_paper_outer_loop.py`
  `compute_step5d_outer_loop` (force_base/u/R_d/e_o, lines 226–275).
- Bridge call site: `tools/kunwei_rtde_bridge.py:1366–1406` (outer loop),
  `:1432–1457` (step5d joint speedj branch), `:778` (`force_t` = live zeroed),
  `:695–719` (`limit_step5d_live_xdot`, separate lin/ang scaling).
- v4 run: `runs/bridge_step5d_strict_rnn_liveprep_v4_20260614_234951/bridge_rtde_500hz.csv`
  - Stage 25.05/25.15/25.3 `step4e_orientation_error_rad = 0.0374`; Stage 25.0
    = 0.9999 → 0.1415.
  - Stage 25.0 recompute row 0: `dot(R_d.z,R_cur.z) = −0.999`, `‖e_o‖ = 0.9999`.
  - Stage 25.0 normal load 6.06 → 0.00 N; `_step5d_outer_xdot_norm` ≈ 5.0.
- v2 run: `runs/bridge_step5d_strict_rnn_liveprep_v2_20260614_231720/bridge_rtde_500hz.csv`
  - Stage 25.0 normal load 18.25 → 65.45 N; `_step5d_outer_xdot_norm` = 5.0000;
    `orientation_error` = 1.0000.
- Prior audit: `ur10e_lab_vault/handoffs/claude_fable5_ur10e_tase_full_audit_report_20260610.md`
  (v20 `wy_sign`/`wz` root cause; P0 = sign + offline replay + no-contact test).

## 7. Verification status of THIS audit

- Code claims: read directly from `step5d_paper_outer_loop.py` and
  `kunwei_rtde_bridge.py`. ✔
- Numeric claims (2.1° vs 178°, `dot = −0.999`, pinned 5.0, v2 65 N / v4 0 N):
  recomputed from the committed run CSVs with the actual outer-loop module. ✔
- Not run: no code was changed; no test re-run; no bridge/contact run. This is a
  diagnosis document only. The fixes in §5 and the P0 in §5.5 are **not yet
  done**.
