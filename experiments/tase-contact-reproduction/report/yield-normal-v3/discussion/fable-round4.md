# Round 4 advisory: what NO-v3 establishes about estimator contamination versus controller dynamics

Role: advisory, non-blocking, no live authority; main owns implementation, integration and acceptance.
Base fd6be9a1; HEAD at receipt time 9d40485a (main's concurrent commit; `tools/contact_yield_*.py`,
`report/yield-normal-v3/` and every config/report file read here are byte-identical between the two, checked
with `git diff`). Development data only; no closed-loop runs, no controller/test/config edits, no commits.
This directory holds read-only postprocessing (`scripts/`) and derived numbers (`data/`). The per-tick
cache is `/tmp/ynv3` (regenerated from the retained receipts by `scripts/extract_ticks_v3.py`; not
retained). All 15 raw receipts (nine NO-v3 cells, four retained comparators, two 1 ms refinements) were
re-hashed against `results.json`/`refinement.json` before use and match (`data/round4-manifest.json`).

Evidence classes: [S] structural (algebra, task/protocol definitions, controller code); [D] data from the
retained closed-loop receipts; [R] numbers reported in earlier reports and hashed but not re-derived from
raw here; [H] hypothesis, falsifiable but not established by these data; [P] statement about physical
hardware, not established by any simulator result. Nothing here is holdout, hardware or acceptance evidence.

## 0. Answers

1. **Established.** Under the combined observer, the matched-nominal recovery time measures the observer's
   own re-convergence, not DSFC's recovery. [S][D] After the tangent release the TCP difference to the
   matched nominal is 0.727 mm per degree of estimate divergence, which is exactly the force-axis leak
   5 N · sin δ / 120 N/m through the fixed path spring; recovery (8.424 s, path-band bound) is reached when
   the divergence falls to 1.8 deg, and the estimate stays frozen at 10 deg until 1.5 s after release
   because the excitation gate is closed while the yielded TCP waits for the reference. The frozen cell gives the same yield (23.0 versus
   22.0 mm) and 0.942 s. For one controller, three observers give 0.94 s (frozen), 2.66 s (legacy, [R]) and
   8.42 s (combined); under one observer (legacy) the three controllers gave 0.46 / 2.66 / 10.93 s [R].
   The observer moves this metric as much as the controller does.
2. **Established.** The tangent-push contamination of the coplanarity residual is geometric, not a gain
   defect. [S][D] The residual on the true normal is (|f_ext| / N) · sin θ with θ the angle between the
   push and the actual sliding velocity (closed form 0.186 versus measured 0.180 RMS, contact part 0.001).
   Under the honest frozen frame θ is 16 deg median and up to 41 deg, reproduced within 5 deg by the
   yielded 21 mm offset rotating with the reference tangent. Under the combined observer the estimate
   chases this moving target at the 0.05 rad/s cap for 42 percent of the hold, tilts 15.4 deg, and the
   tilt itself widens θ to 42 deg median: the loop amplifies its own contamination.
3. **Not established.** Whether the normal-hold penalty (3.834 versus 0 s) is estimator- or
   controller-caused. [D] It is force-band bound: post-release load excursions relative to nominal reach
   0.857 N (combined) against 0.454 N (frozen), with a 0.5 N band. The frozen cell passes by 0.046 N,
   smaller than the 0.12 to 0.19 N maximum force differences NO-v3's own 1 ms refinement recorded for the
   two nominal across-prior cells [R], so the categorical 0 s could flip under refinement. The estimate diverges at most
   1.7 deg but moves at 2.3 to 2.6 deg/s RMS (near cap) throughout the hold, release and four seconds
   beyond, while the force loop's normal motion is 1.4 times the frozen cell's. Cause and effect are not
   separable in a closed loop from one pair of runs.
4. **One more observer design is not justified now** (section 7). The contamination channel is structural
   and any gate that could suppress it needs either a declared prior cone or an identified friction bound,
   both protocol decisions. The next bounded experiment should be the controller comparison under a fixed
   versioned observer (section 8): four cells minimum, eight with the interaction arm, no new observer,
   no changed criteria.

## 1. Receipts and method

| Cell | SHA-256 (12) | Use |
|---|---|---|
| combined-mild-approach | bed69746e5e1 | matched nominal for the combined hold cells; prior correction |
| combined-normal-hold / combined-tangent-hold | 5fe0b6f04ca3 / e02b1820c1b8 | recovery decomposition, contamination |
| frozen-normal-hold / frozen-tangent-hold | 063f027dbbdb / a85285e1cc35 | controller-only references |
| retained-approach (P0-v1) | 234fe77c3966 | matched nominal for the frozen hold cells |
| combined-mild-along / -across, motion-only-mild-across | 918158d0622e / d754c461bdee / ca931e3d8d4f | prior correction, progress, offset identity |
| retained-along_10deg / -across_10deg / -strong-frozen, combined-strong-approach | 4391cf7c1961 / 0553ba2c1396 / 78fbd04b36c2 / 23e9b8d4b23a | comparators, curvature |

Per-tick quantities are taken from `rows` (evaluator truth: true normal, true load, external force) and
`records[*].result` (controller-side filtered force, measured velocity, estimate, gates, coplanarity
residual, commanded normal speed). Evaluator truth is used only to label mechanisms; nothing is fed back.
`scripts/analyze_round4.py` reproduces every number below from the cache; its output is
`data/round4-analysis.json`. The report's recovery values (3.834, 8.424, 0, 0.942 s) were reproduced
exactly with `compare_pair`'s definition before any interpretation.

## 2. What the matched-nominal recovery metric measures under an adaptive observer

[S] `compare_pair` compares the disturbed trial with a same-method nominal trial and reports the first
post-release time from which the TCP difference stays within 2 mm and the true-load difference within
0.5 N. With a frozen observer both trials carry the identical estimate, so the difference is the
controller's response alone. With an adaptive observer the two trials carry different estimate
histories; while they differ by δ, the controller regulates force along two different axes, and the
fixed path spring balances the tangential leak of the 5 N normal force: the steady TCP difference is
5 · sin δ / 120, i.e. 0.727 mm/deg, independent of the control law.

[D] All four hold cells, matched nominal as in results.json:

| Cell | Recovery s | Binding band | Yield peak mm | Post-release max dist mm | Post-release max abs delta-load N | Estimate versus nominal at release / at recovery deg | Slope dist versus divergence mm/deg |
|---|---:|---|---:|---:|---:|---|---:|
| combined-tangent-hold | 8.424 | path | 22.03 | 8.48 | 0.18 | 10.2 / 1.8 | 0.727 |
| frozen-tangent-hold | 0.942 | path | 23.04 | 4.91 | 0.00 | 0 / 0 | n/a |
| combined-normal-hold | 3.834 | force | 4.63 | 1.47 | 0.86 | 1.7 / 0.9 | 0.746 |
| frozen-normal-hold | 0.000 | none | 3.19 | 0.27 | 0.45 | 0 / 0 | n/a |

Tangent, 1 s bins (PATH clock; release ramp 30.5 to 35.5 s):

| PATH s | dist to nominal mm | est. vs nominal deg | gate open | tangent speed in est. frame mm/s (median) | measured progress mm/s |
|---:|---:|---:|---:|---:|---:|
| 30 | 14.8 | 15.0 | 1.00 | 4.1 | 3.7 |
| 32 | 15.0 | 12.7 | 0.52 | 2.0 | 2.8 |
| 33 | 15.2 | 10.4 | 0.01 | 0.6 | 0.2 |
| 34 | 12.4 | 10.0 | 0.00 | 0.6 | -0.5 |
| 35 | 8.8 | 10.1 | 0.00 | 0.3 | 0.1 |
| 36 | 6.9 | 10.2 | 0.00 | 1.1 | 1.1 |
| 37 | 7.8 | 10.1 | 0.39 | 2.0 | 2.0 |
| 38 | 7.8 | 8.4 | 0.90 | 2.6 | 2.6 |
| 40 | 7.7 | 6.4 | 1.00 | 4.2 | 4.2 |
| 42 | 4.6 | 3.7 | 1.00 | 3.3 | 3.2 |
| 43.9 (recovery) | 2.0 | 1.8 | 1.00 | | |

The estimate does not move between PATH 32.5 and 37 (rate 0.0 deg/s median) because the excitation gate
is closed: the TCP, pushed 20 mm ahead along the direction of travel, is nearly stationary in the
estimated tangent plane (0.3 to 1.1 mm/s against the 2 mm/s gate) while the reference catches up. The
frozen cell shows the same closure (PATH 32 to 36) and the same wait, and recovers 0.94 s after release
because there is nothing to un-learn. Once the gate reopens the combined estimate converges at 1 to
2.5 deg/s and recovery follows the 0.727 mm/deg line to the 2 mm band at 1.8 deg. DSFC's yielding is
unchanged between the two observers (22.0 versus 23.0 mm peak, both against the same 2.5 N push), so the
7.5 s difference is entirely observer state plus gate closure.

Normal, 0.5 s bins after the ramp end (combined; frozen in parentheses):

| PATH s | delta-load mean N | delta-load max abs N | est. vs nominal deg | estimate rate deg/s RMS | commanded normal speed mm/s RMS |
|---:|---:|---:|---:|---:|---:|
| 35.5 | +0.30 (+0.07) | 0.86 (0.45) | 1.49 (0) | 2.6 (0) | 1.19 (0.51) |
| 36.5 | -0.23 (+0.02) | 0.75 (0.37) | 1.38 | 2.3 | 0.95 (0.42) |
| 37.5 | +0.08 (-0.08) | 0.69 (0.31) | 1.13 | 2.2 | 0.75 (0.40) |
| 38.5 | +0.07 (+0.09) | 0.58 (0.24) | 0.96 | 2.1 | 0.68 (0.35) |
| 39.5 | -0.15 (-0.06) | 0.47 (0.18) | 0.84 | 2.3 | 0.63 (0.25) |
| 40.5 | +0.14 (+0.01) | 0.37 (0.15) | 0.68 | 1.9 | 0.51 (0.15) |

The path band is never violated in either normal cell. The force band is violated for 871 ticks between
PATH 35.5 and 39.33 s in the combined cell and never in the frozen cell, whose worst excursion is 0.454 N.
Both cells show the same kind of post-ramp load ringing (the controller re-presses as the 2.5 N pull
disappears); the combined cell's is roughly twice as large and decays with the estimate's motion. [H] The
estimate's near-cap motion during the whole hold (section 4) is the most plausible amplifier, through the
projector rotation re-mixing normal and tangent commands each tick, but the reverse causality (larger
force ringing driving the estimate through n·v) is equally consistent with these two runs.

Consequence for the research question [S][D][R]: the DSFC tangent recovery is 0.94 s under the frozen
honest frame, 2.66 s under the legacy force-following frame (TR-v1, whose frame the push itself rotated by
about 20 deg in round 2) and 8.42 s under NO-v3. The TR-v1 controller ranking on this metric (SFC 10.93,
DSFC 2.66, MSFC-g50 0.46 s, stiff, legacy observer) was therefore measured through an observer whose own
push response is of the same order as the differences it was meant to resolve. Nothing in NO-v3 tells us
whether that ranking survives an honest frame; only running the other two controllers under the same
observer can.

## 3. Tangent push: the contamination is geometric and closed-loop amplified

[S] With ideal Coulomb contact f = N n_out - mu N r(v_t) + f_ext and v = v_t + a n, the friction and
normal terms give f x v in the binormal direction, so the true-normal residual is
r = n·(f x v)/|f x v| ≈ (|f_ext| / N) sin θ, where θ is the angle in the tangent plane between the push
and the actual sliding velocity. A push collinear with sliding is invisible to the residual; a transverse
component is indistinguishable, at one instant, from a tilt of asin(r).

[D] Hold window 20.5 to 30.5 s, gated ticks (5000 in each cell):

| Cell | θ push vs actual sliding deg (p10 / median / p90) | r on true normal RMS | external part | contact part | closed form (f_ext/N) sin θ | r on estimate RMS | tangential force in est. frame RMS N | mu N N |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| frozen-tangent-hold | 3.6 / 15.8 / 39.3 | 0.180 | 0.179 | 0.001 | 0.186 | 0.188 | 1.98 | 0.75 |
| combined-tangent-hold | 5.2 / 42.0 / 54.3 | 0.293 | 0.292 | 0.001 | 0.313 | 0.184 | 1.66 | 0.73 |

The push direction equals the reference tangent to numerical precision (max deviation 0.0 deg), as the
protocol defines it. The nominal-window residual on the true normal is 0.001 to 0.002 RMS in both cells,
so the normal push is not the issue; the in-plane push is. Round 3 left open whether the 17 deg
push-versus-sliding deviation seen under the legacy frame was a frame artifact. It is not: under the
frozen honest frame the median is 15.8 deg, and after the yield transient it is reproduced by geometry.

[S][D] A TCP displaced by d along a direction that rotates at ω has a transverse velocity ω d relative to
that direction, so tan θ ≈ ω d / |v_ref| once the yield transient is over. Frozen cell, 1 s medians:

| PATH s | reference-tangent rotation rate deg/s | offset d mm | reference speed mm/s | predicted θ deg | measured θ deg |
|---:|---:|---:|---:|---:|---:|
| 24 | 6.6 | 22.8 | 3.1 | 40 | 41 |
| 25 | 5.3 | 21.6 | 3.4 | 30 | 34 |
| 26 | 4.1 | 20.9 | 3.7 | 22 | 28 |
| 27 | 3.0 | 20.8 | 4.0 | 16 | 21 |
| 28 | 2.2 | 20.7 | 4.2 | 11 | 15 |
| 29 | 1.4 | 20.4 | 4.3 | 6 | 10 |
| 30 | 0.7 | 20.1 | 4.4 | 3 | 7 |

During the transient (PATH 20.5 to 23) the TCP slides along the push at the 10 mm/s tangent cap and θ is
2 to 8 deg. The contamination therefore peaks exactly when the yield has settled and the reference is
turning, which on this figure-eight is the first half of the hold. [H] A push fixed in the base frame
(a person pushing in one direction) would be transverse to sliding for most of the hold for the same
reason, with θ growing as the reference turns away from it; the simulator does not implement that case.

[D] In the combined cell the same geometry predicts θ decaying from 38 to 2 deg over PATH 24 to 30, but the
measured median stays at 54 to 22 deg. The difference is the estimate: it tilts along the push
(-10.0 deg) and along the push binormal (+11.4 deg) by PATH 29 (15.4 deg total at 29.8 s), moving at the
0.05 rad/s cap for 42 percent of gated hold ticks (maximum 2.865 deg/s = cap). The tilted frame leaks
5 N · sin 15 deg ≈ 1.3 N of normal force into the tangent plane and re-projects the feedforward, which
changes the sliding direction and keeps the residual large (0.184 RMS on the estimate, versus 0.188 in the
frozen cell where nothing responds). The yield also partly unwinds during the hold (21.8 to 14.7 mm,
frozen 20 to 23 mm), i.e. the tilted frame opposes the push with the leaked normal force. None of this
requires a gain change to explain, and lowering the coplanarity gain would only slow both the correction
in section 5 and the contamination here in the same proportion.

## 4. Normal push: what is identifiable and what the estimate does

[D] During the 2.5 N outward pull the controller holds the measured force norm at 4.95 to 5.04 N in both
normal cells while the true contact load is 2.44 to 2.53 N. The coplanarity residual on the true normal
stays clean (combined intervention-window RMS 0.022 against 0.011 nominal; the pull is along n and keeps
f in the (n, v) plane), consistent with the round 3 identity. The motion residual is contaminated: the
force loop yields with 0.8 to 1.0 mm/s RMS of true normal velocity against 2.7 to 4.6 mm/s of tangential
speed, giving an energy ratio (n·v)^2 / |P v|^2 of 0.05 to 0.08 during the hold in both cells (frozen
0.66 to 0.79 mm/s, ratio 0.02 to 0.08). The combined estimate error rises from 1.9 to 2.7 deg and falls
back to 1.6 deg by the release; its rate is 2.3 to 2.5 deg/s RMS through the whole hold, the ramp and
until PATH 40, against 0.03 to 0.2 deg/s in its own nominal. Round 3's predicted jitter floor at the cap
is what this looks like: the rectified n·v pushes, the coplanarity term restores, and the sum sits near
the cap without net drift. The state stays within 2.7 deg of truth, so NO-v2's runaway does not recur at
this gain, but the estimate is never at rest while the force loop is active.

[S] What the wrist force cannot say here: the measured 5 N is the sum of a 2.5 N contact load and a
2.5 N external pull, and no combination of f and v distinguishes it from a 5 N contact load with no
pull. Friction magnitude drops with the contact load (mu N ≈ 0.37 N instead of 0.75 N); a friction model
could infer N from |f_t| / mu, but only with an identified mu. The normal direction remains identifiable
through coplanarity; the normal load does not.

## 5. Prior correction, path offset, progress and entry

[S][D] Path offset identity. In every cell the tangential path error in the estimated frame equals the
tangential force in that frame divided by 120 N/m, with a residual of 0.17 to 0.56 mm RMS after PATH 5 s:

| Cell | path offset in est. frame mm RMS | tangential force in est. frame N RMS | tilt-leak proxy 5 sin(err) N RMS | identity residual mm RMS |
|---|---:|---:|---:|---:|
| retained-approach (frozen) | 4.99 | 0.59 | 0.12 | 0.17 |
| combined-mild-approach | 5.15 | 0.61 | 0.10 | 0.35 |
| retained-across_10deg (frozen) | 8.31 | 1.00 | 0.84 | 0.20 |
| motion-only-mild-across | 6.54 | 0.78 | 0.65 | 0.32 |
| combined-mild-across | 5.15 | 0.62 | 0.19 | 0.39 |
| retained-along_10deg (frozen) | 9.20 | 1.10 | 0.82 | 0.29 |
| combined-mild-along | 5.08 | 0.60 | 0.06 | 0.18 |

The 0.59 to 0.62 N in the approach cells is the regularised friction at 2 to 4 mm/s; the tilted frozen
priors add the 0.8 N leak and the combined observer removes it. Path RMS differences between observers on
this task are the leak divided by Kp, not a property of the control law. Reading path RMS as a controller
metric under different observers, or under one adapting observer, would mis-attribute this.

[D] Measured progress by reference window (lateral-dominant windows from round 3):

| Cell | 0 to 12 s | lateral 12 to 19.5 s | 19.5 to 43.4 s | lateral 43.4 to 50.9 s | 50.9 to 62.8 s | total |
|---|---:|---:|---:|---:|---:|---:|
| retained-approach | 0.820 | 0.813 | 0.926 | 0.809 | 0.860 | 0.872 |
| combined-mild-approach | 0.828 | 0.805 | 0.928 | 0.813 | 0.867 | 0.875 |
| retained-across_10deg | 0.824 | 0.809 | 0.923 | 0.799 | 0.860 | 0.870 |
| motion-only-mild-across | 0.794 | 0.806 | 0.961 | 0.807 | 0.906 | 0.890 |
| combined-mild-across | 0.757 | 0.805 | 0.920 | 0.813 | 0.867 | 0.857 |
| retained-along_10deg | 0.968 | 0.803 | 0.923 | 0.804 | 0.858 | 0.900 |
| combined-mild-along | 0.833 | 0.808 | 0.944 | 0.813 | 0.867 | 0.883 |

Every mild cell loses 19 to 20 percent in both lateral windows regardless of observer or prior; the gate
is closed there (0.00 to 0.05) and the 5 to 6 mm friction lag rotates through 180 deg. This is the honest
frame's cost with a fixed Kp and is identical across observers. The combined observer's lower total in
the across cell comes entirely from the first window (0.757 versus 0.828) while it removes the 9 deg
prior; a persistent tilt can raise measured progress (frozen along prior 0.968 in the first window,
motion-only 0.961 in the third), because the leaked normal force acts as a fixed-direction push that
alternately assists and opposes the feed. [H] Progress is therefore not monotone in estimate quality and
should not be read as an observer-quality metric on this task; its observer-related variation here is at
most 0.03 against a 0.13 friction-lag floor.

[D] Entry adaptation (1 s entry, estimator active from the first tick, gated 35 to 67 percent of ticks):

| Cell | initial error deg | PATH-start error deg | change | gated entry energy ratio |
|---|---:|---:|---:|---:|
| combined, approach prior (three identical cells) | 0.917 | 1.774 | +0.86 | 0.198 |
| combined, strong surface, approach prior | 0.917 | 1.926 | +1.01 | 0.198 |
| combined, along prior | 9.189 | 8.514 | -0.68 | 0.080 |
| combined, across prior | 9.625 | 9.059 | -0.57 | 0.102 |
| motion-only, across prior | 9.625 | 9.424 | -0.20 | 0.102 |
| frozen (two cells) | 0.917 | 0.933 | +0.02 | 0.195 |

With a good prior the entry updates make the estimate worse by 0.9 deg: approach motion is one quarter
normal velocity (median |n·v|/|P v| 0.24), and the motion residual reads it as a tilt. With a 10 deg prior
the entry helps by about 0.6 deg, and the coplanarity term supplies most of it (motion-only 0.2 deg).
Either way the effect is small compared with the 8 to 9 deg corrected during PATH, and the study's
separation of initial from PATH-start error is the right reporting.

[D] Strong curvature. The combined observer's 5.95 deg RMS is 9.4 deg of across error in the first
lateral window and 4.4 deg in the second, where the gate is closed (0.00 / 0.015) and the estimate cannot
move while the true normal sweeps through its largest excursion, plus 4.5 to 5.6 deg RMS in the along
windows. [S] At
gain 0.3/s a normal rotating at κ v ≈ 8 /m × 4 mm/s = 0.032 rad/s is followed with a first-order lag of
about 0.032/0.3 = 6 deg, so the along-window error is the gain-limited lag, not contamination. The
"not solved" verdict in the README is correct, and its cause is excitation (gate) and gain, which is
also why simply raising the gain is not an answer: the contamination in section 3 scales with it.

## 6. Physical identifiability during external intervention

[S] The controller sees f (filtered wrist force), v (TCP velocity), position versus reference, and their
histories. The wrist reports the sum of contact normal force, friction and any external force. From that:

- Normal load is not identifiable during a normal push (section 4). The 5 N regulation target is met by
  2.5 N of contact plus 2.5 N of pull. A friction model |f_t| = mu N would identify N, but mu is not
  identified on hardware [P], and in the simulator its regularised value already differs from 0.15 at
  2 to 4 mm/s (0.59 N versus 0.75 N in section 5).
- Normal direction remains identifiable through coplanarity during a normal push, and through the motion
  residual during a tangent push (n·v is force-immune). Each residual is clean exactly when the other is
  contaminated: normal pushes inject normal velocity into n·v (energy ratio 0.05 to 0.08 here), in-plane
  pushes inject (|f_ext| / N) sin θ into the coplanarity residual (0.18 to 0.29 here).
- A single coplanarity residual value r cannot separate "the normal is tilted by asin r" from "an in-plane
  force has a transverse component N r". Over time the two differ: a tilt is fixed in the base frame while
  the push residual rotates with the sliding direction. On this task the sliding direction turned by
  50 deg during the hold, which is why the estimate chased rather than converged; on a straight segment
  the two would be indistinguishable for the whole hold. This is an observability statement, not a
  detector design.
- The one quantity contact friction cannot produce is tangential force above mu N. Here the tangential
  force in the estimated frame during the tangent hold is 1.66 to 1.98 N RMS against mu N = 0.73 to
  0.75 N, so a friction-bound test would have fired in the simulator. It is realistically observable
  (f, the estimated normal, and a declared mu-hat) but has two costs: it needs mu-hat, and a wrong estimate
  leaks 5 sin δ into the same quantity (0.87 N at 10 deg, more than mu N), so its dead zone is at least
  mu-hat N + 5 sin(prior cone) ≈ 1.6 N at a 10 deg cone. Pushes below that are indistinguishable from
  friction plus prior error, and would remain a declared non-yield zone. [P] On the robot, force bias and
  gravity-compensation error enter the same test directly.
- Simulator intervention labels (`external_force_base_n`, `scenario`) are evaluator-only. Every number in
  sections 2 to 5 that used them is a mechanism label, not something a controller could compute.

## 7. Is one more observer design justified?

No, not from this evidence. The reasons, in order of weight:

- The tangent contamination is a property of the observation model under an in-plane push that is not
  collinear with sliding (section 3). Any residual-only gate (cone, magnitude, sign) faces the ambiguity in
  section 6 and, with a rotating push, a residual that sits at 0.18 to 0.29, i.e. 10 to 17 deg
  equivalent, which is inside any prior cone wide enough to be useful. Round 2 already showed that gating
  the motion residual biases it; the same applies to gating the coplanarity residual on its own value.
- A friction-bound intervention gate is the only physically grounded option and is a protocol decision
  (declared mu-hat and prior cone, declared non-yield zone), not a tuning change. It should not be
  introduced as an observer parameter and then read as validated by the simulator's ideal mu.
- Freezing the estimate during suspected intervention would be an "intervention reset" by another name,
  which the protocol excludes, and it needs the same gate.
- The normal-hold penalty is at the band edge (0.454 versus 0.5 N) and within reported step sensitivity;
  designing against it now would be designing against a threshold artifact.
- The step refinement covered only the two across-prior nominal cells; none of the intervention cells has
  a 1 ms check, so the recovery numbers that motivated a redesign are themselves unchecked numerically.

What NO-v3 does justify keeping: the combined update as the versioned development observer for prior
correction (section 5) with its negative intervention results attached, exactly as the README states.

## 8. Smallest evidence-producing next action

Compare the three controllers under one fixed, versioned observer before any observer change. The DSFC
cells already exist under both the frozen prior and NO-v3, so the increment is SFC and MSFC only.

Minimum (four cells, existing scenarios, existing metrics, existing gates, nothing retuned): SFC and
MSFC × {nominal, sustained_release_tangent}, stiff_low_mu, DSFC mechanics otherwise unchanged, frozen
observer (`yield_normal_frozen_prior_v1`, approach prior), 2 ms / 8 substeps, full cycle. Use the same
SFC and MSFC parameter sets TR-v1 used so the result is comparable to the only existing cross-controller
data. This arm isolates controller dynamics from observer state by construction (estimate divergence is
identically zero) and answers whether the TR-v1 ranking (10.93 / 2.66 / 0.46 s) exists at all under an
honest frame.

Interaction arm (four more cells, same batch if budget allows): the same four under NO-v3. Together with
the existing DSFC cells this gives a 3 × 2 observer-by-controller table for tangent recovery and yield.

Falsifiable expectations, from sections 2 and 3 (development predictions, not acceptance thresholds):

- Frozen arm: steady yield against the 2.5 N push is f_ext / Kp ≈ 21 mm for every law (DSFC measured
  23.0 mm peak), so the laws differ only in the transient and in recovery. If SFC and MSFC recoveries lie
  within the 0.9 to 2.7 s band already seen for DSFC across observers, the TR-v1 spread was
  observer-mediated; if SFC stays near 10 s under the frozen frame, the controller difference is real.
- NO-v3 arm: recovery is about the time for the estimate divergence to fall to 2.75 deg (2 mm at
  0.727 mm/deg) after the gate reopens, so it should be nearly law-independent except through the yield
  magnitude and the length of the gate-closed wait. A law that yields less closes the gate for less time
  and should recover sooner by that amount alone.
- If the ranking differs between the two arms, the interaction must be modelled before either an observer
  or a controller is chosen; if it agrees, the observer question can be pursued separately.

Report alongside the existing metrics, without changing any criterion: the estimate-versus-nominal angle
at release and at recovery, the gate-open fraction in the post-release window, and the post-release
maximum TCP and load excursions (the band-edge margins that turned 0.454 versus 0.857 N into 0 versus
3.834 s). All of these are already in the receipts. A secondary check, two runs, is a 1 ms refinement of
the two normal-hold cells to see whether the 0.046 N frozen margin survives; it changes an interpretation,
not the next decision, and comes after the four-cell arm.

Not recommended: a new observer variant, a residual or intervention gate, a compliant-material arm or the
short-pulse scenarios in this step, lowering the 2 mm/s gate (the recovery wait in section 2 happens
below it for a reason), or any per-cell retuning.

## 9. What this round does not establish

- Nothing here validates the ideal Coulomb contact, the servo model or the joint-velocity clip; the
  identities in sections 3 and 5 hold in that model and are the reason its intervention cells look the
  way they do. [P] Stick-slip, force bias, tool compliance and an orientation-dependent contact patch are
  outside every number above.
- The recovery decomposition is exact for the path band (a Kp identity) and only descriptive for the
  force band; the normal-hold cause is open (section 2).
- All cells are single deterministic runs. The intervention cells have no step-refinement check; the
  nominal across-prior cells showed 0.44 to 0.49 mm and 0.12 to 0.19 N maximum differences at 1 ms [R],
  below the 7.5 s / 8 mm tangent effects in section 2 but above the 0.046 N frozen normal-hold margin.
- Numbers marked [R] (TR-v1 recoveries, NO-v3 refinement maxima, the round 2 legacy-frame rotation) are
  taken from hashed reports and were not re-derived from their raw receipts in this round.
- No observer or controller is declared better by anything above; the frozen prior is an instrument for
  isolating controller dynamics, not a candidate for the unknown-surface task.

## Reproduction

```bash
# from the experiment root; cache under /tmp/ynv3 (not retained)
D=report/yield-normal-v3/discussion
mkdir -p /tmp/ynv3
for c in combined-mild-approach combined-normal-hold combined-tangent-hold frozen-normal-hold frozen-tangent-hold \
         combined-mild-across motion-only-mild-across combined-mild-along combined-strong-approach; do
  .venv-contact-six/bin/python $D/scripts/extract_ticks_v3.py runs/yield-normal-v3/$c.json.gz /tmp/ynv3/$c.npz
done
.venv-contact-six/bin/python $D/scripts/extract_ticks_v3.py runs/yield-observer-prior-v1/DSFC-stiff_low_mu-approach.json.gz /tmp/ynv3/retained-approach.npz
.venv-contact-six/bin/python $D/scripts/extract_ticks_v3.py runs/yield-observer-prior-v1/DSFC-stiff_low_mu-across_10deg.json.gz /tmp/ynv3/retained-across_10deg.npz
.venv-contact-six/bin/python $D/scripts/extract_ticks_v3.py runs/yield-observer-prior-v1/DSFC-stiff_low_mu-along_10deg.json.gz /tmp/ynv3/retained-along_10deg.npz
.venv-contact-six/bin/python $D/scripts/extract_ticks_v3.py runs/yield-surface-excitation-v1/DSFC-stiff_low_mu-frozen_prior.json.gz /tmp/ynv3/retained-strong-frozen.npz
.venv-contact-six/bin/python $D/scripts/analyze_round4.py > $D/data/round4-analysis.json
.venv-contact-six/bin/python $D/scripts/manifest_round4.py
```
