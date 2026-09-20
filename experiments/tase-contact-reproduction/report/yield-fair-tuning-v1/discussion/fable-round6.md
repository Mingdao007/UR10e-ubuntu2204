# Round 6 advisory: an executable selection contract for the 24-pair fair comparison

Role: advisory, non-blocking, no live authority; main owns goal, integration, acceptance and hardware.
First of at most two iterations this round. HEAD at session start 6f041ef6; main committed five times
during the session (b13168f6 cycle-seam repair, bc2d016a the three-method proposer, aeb9b61d paired ledger,
1f29c864 route anchor counterexample, dbf91c97 native preparation binding at receipt time). The proposer and
its config were read at bc2d016a; the three later commits were not read. The receipt records the start and
end HEADs and the exact tool/config files read. Development data only;
no runs, no edits outside this directory, no Git writes, no subagents, no devices, no literature. Round 5
and its adjudication were read and are preserved unchanged. All 31 raw receipts used numerically here were
re-hashed against their studies' results/protocol files and match (`data/round6-receipt-manifest.json`);
report/tool inputs that changed since round 5 are listed in `round6-input-receipt.json` with current
hashes rather than presented under the round-5 hashes. The tick caches are `/tmp/yfp6` (rows only, from
`scripts/extract_rows_v6.py`) and the round-5 cache `/tmp/yfp5`, both regenerable and not retained.

Evidence classes as in round 5: [S] structural (code, protocol, algebra); [D] re-derived here from retained
receipts; [R] reported in hashed reports, not re-derived; [H] hypothesis; [P] physical, not established by
any simulator result.

## 0. Answers

1. **Budget (Q1).** Spend all 24 units of every method on one pre-declared training cell: stiff_low_mu,
   mild surface (0.8, 0.4)/m, approach prior, cold preparation, `sustained_release_oblique`, 2 ms controller,
   8 plant substeps, NO-v3 observer at the OT-v1 parameters. One unit is one sealed nominal/disturbed pair at
   that cell, which is what the proposer committed at bc2d016a already requires (`training_cell_id`).
   Balanced or contextual allocation across scenarios is incompatible with that proposer's own contract:
   a candidate's aggregate objective is never observed unless it is run on every scenario, which costs one
   pair per scenario (the budget silently multiplied), and otherwise the "repeat observed incumbent" block
   has nothing observed to repeat. Keep the four repeats literal; in this simulator they are bitwise replays
   and should be reported as determinism evidence with no confidence interval. What the design can claim:
   "equal-budget best feasible candidate per method on this cell, and pre-registered transfer to the holdout
   blocks". What it cannot claim: per-scenario optimality, amplitude generality, physical benefit.
2. **Selection (Q2).** Feasibility first, then one scalar. Feasibility is a set of bands declared now from the
   NO-v3 seed nominals (no candidate worse than the untuned baseline seed on any nominal descriptor, with the
   existing step-resolution as margin: path RMS ≤ 5.48 mm, progress ≥ 0.850, attitude RMS ≤ 2.81 deg, load MAE
   ≤ 0.127 N, peak ≤ 6.27 N, no sample below 1 N) plus disturbed-trial guards (no failure, true load never below
   1 N or above 8 N, progress ≥ 0.850). The objective is J = ∫|Δload|/0.5 N + ∫‖Δx‖/2 mm (post-release only)
   + ∫|Δattitude|/0.05 rad, every term against the matched nominal, unit seconds. It is nonnegative and
   threshold-free, it uses the two existing recovery bands plus the frozen observer's own rate cap, and on
   every existing development pair its ordering is stable across twelve load/path weightings; the one
   weight-sensitive ordering (SFC versus DSFC in the tangent hold at half the attitude band) is shown in
   section 3, which is exactly why the weights must be frozen before unit 1. The 2 mm/0.5 N recovery time is
   reported unchanged and is never the objective. Precision judgment: the comparison runs at the common
   module's 5.2 to 5.4 mm path RMS and 0.86 progress; the legacy observer's 1.2 to 1.5 mm came with 6.5 deg
   attitude error, progress above one, and 8 to 14 s of QP task scaling per cycle. No arm can support a
   "precise contact" claim from this campaign; that needs common-module work outside it.
3. **Numerical checks (Q3).** Two separated checks per frozen incumbent, run after unit 24 and never fed back:
   controller 1 ms at plant 0.25 ms, and controller 2 ms at plant 0.125 ms, each with its own matched nominal.
   Qualification for any recovery-time statement uses the existing bands as the pointwise bound (≤ 0.5 N and
   ≤ 2 mm between base and each refined setting). Disclosed: of seven already-reported step differences this
   admits the g50 pulse (0.13 N) and the NO-v3 nominal (0.19 N) and rejects the SFC/NO-v3 normal hold (1.005 N)
   and g100 (3.6 N). Benefit statements use a paired criterion: the cross-arm difference must keep its sign at
   all settings and its smallest magnitude must exceed its variation across settings. On the pulse pair this
   resolves peak load (+0.147/+0.150 N) and J (+0.74/+0.81 s) and does not resolve recovery time
   (+0.05/+0.32 s). Offline holdout is n = 1 deterministic per cell with no confidence interval; the protocol's
   five repeats are a hardware requirement and stay one.
4. **Holdout (Q4).** Five pre-registered blocks, 112 trials, about 2.5 worker-hours at 2 ms, five arms (three
   tuned incumbents, MSFC-identity at MSFC's incumbent triple, SFC_RADIAL at SFC's incumbent triple):
   direction/shape transfer on the training material; the compliant material (never run under NO-v3); strong
   curvature (6, 8)/m; along- and across-feed 10 deg priors; and warm preparation, which is the runner's
   existing 2 s stationary baseline and the only state-preparation factor that exists (MSFC's memory has
   0.19 and 0.20 s time constants, so there is no cross-cycle warm state to test). Cells already viewed with the
   seeds are labelled and carry weaker claims. Stop rules, cost and missing assumptions are in section 5.

## 1. Inputs, what changed since round 5, and the proposer as committed

Round 5's five corrections are adopted: finite-range wording; the fixed 0.5 s envelope window (J does not
use envelopes); separated controller-step and plant-step checks; no claim that the wrist-sum peaks are
unavoidable under every shared controller; recovery bands relative to the matched nominal.

New evidence since round 5 [R]: `report/yield-coefficient-equivalence-v1`. Native DSFC at MSFC g50's six
mechanical coefficients equals identity-metric MSFC to 4.6e-17 m/s at 1, 2 and 3 ms under rotating, held,
pulsed and released prescribed forcing; snapshot replay is exact; the MSFC structure norm reaches 0.31, so
memory evolves. The legacy six-law tuner fixes DSFC at a = 1.2, p = 0.1 while the full-task laws use 0.05
and 0.5; its bounds admit the g50 point but it would search the wrong law. This is the execution counterpart
of round 5's structural statement and is accepted as such; it says nothing about closed-loop contact.

Proposer at bc2d016a, read only [S]: one `training_cell_id` and one `selection_contract_id` per instance;
eight shared mechanical triples (the three FT-v1 seeds in SFC/DSFC/MSFC order, then five scrambled Sobol
points from seed 20260920), twelve EI units (fixed Matérn-5/2, lengthscale 0.35 in the unit cube, noise-free
GP with numerical jitter, EI over a shared 512-point pool), four repeats of the discovery incumbent (minimum
objective among completed, `nominal_feasible` rows; repeat rows cannot change it); bounds m [4, 16] linear,
mu [40, 2500] log, g [0.005, 0.2] log; DSFC/MSFC fixed a = 0.05, p = 0.5; MSFC memory active with
λ_min = 0.0269; the caller supplies a nonnegative `objective` and a boolean `nominal_feasible`; failed units
consume an ordinal without an objective; the config declares `objective_definition: null`. The GP is
noise-free, which matches a deterministic simulator and is also why literal repeats give it no information.

Seeds inside the bounds [D]: unit-cube coordinates SFC (0, 0.55, 0.64), DSFC (0, 0.50, 0.69), MSFC
(0, 0.83, 0.77). All three sit on the m = 4 boundary; headroom above the MSFC seed is ×2.0 in mu and ×2.3 in
g. If an incumbent lands on a bound, the report must say the bound was active; bounds are not extended
after the fact.

Receipts used numerically (31, all hash-matched):

| Group | Receipts (SHA-256 first 12) | Use |
|---|---|---|
| OT-v1 / NO-v3 nominals and holds, NO-v3 observer, 2 ms | SFC 2c786a2ad520 / ac695e565256 / 53f7e2ef79c9; DSFC bed69746e5e1 / 5fe0b6f04ca3 / e02b1820c1b8; MSFC 8b490c06bc68 / 7079dae875ca / bbf2862bbc17 | bands, objective demonstration, attitude windows |
| Legacy-observer nominals | SFC 042934b294bb, DSFC eadaddc8080a, MSFC 6b1a330dc527 | common-module tradeoff |
| NO-v3 DSFC factor cells | along 918158d0622e, across d754c461bdee, strong 23e9b8d4b23a | holdout factor levels |
| FT-v1 / FM-v1 / P0-v1 frozen nominals and tangent holds | nom 4802cd3c4a5d / 234fe77c3966 / b953f4041fa4 / 6d415aa1c394; tan c7c5d28b7805 / a85285e1cc35 / e764b57ab98a / a883afdf6b16 | objective on a path-bound scenario |
| FP-v1 pulses, 2 ms and 1 ms | 5944e27df2e6, b8cfc3afe337, b5ff75ba1ba6, 1f507fae4d44; c597127722ab / 6db73f5f915f, 5769d72f25e0 / b429de054c55 | objective on a force-bound scenario, step sensitivity |

Every stored metric in these receipts was recomputed from `rows` and matches to 1e-18 (`data/round6-analysis.json`,
`nominal_recomputed`). The numbers main quoted for the NO-v3 nominals (SFC 0.1119 N, 5.43 mm, 0.0447 rad,
0.855; DSFC 0.0715, 5.22, 0.0203, 0.875; MSFC 0.0609, 5.22, 0.0228, 0.873) are exactly these receipts [D].

## 2. Q1: one training cell, one pair per unit, the four repeats kept literal

### 2.1 Why the budget cannot be spread over scenarios inside this proposer

[S] The proposer's incumbent is `min(objective)` over completed feasible rows and its repeat block replays
that observed row. An objective aggregated over k scenarios is observed only if the candidate is run on all
k, at k pairs per unit; the unit definition "one nominal/disturbed pair" then means the budget is 24k pairs
per method, which is the silent multiplication main wants excluded. A contextual GP that observes each
candidate on one scenario and predicts the aggregate has no observed incumbent to repeat and, with 20
discovery points over three continuous dimensions plus a four-level context, has fewer than six points per
context; its EI is model extrapolation, not the schedule the protocol defines. Sequential blocks (twelve
units on one scenario, twelve on another) halve the discovery budget per condition and make "incumbent"
ambiguous. None of these preserves 8 + 12 + 4 as written. The single training cell does, with the proposer
as committed and with no relabelling.

The explicit alternative, if main wants material-specific tuning, is a second formal campaign on the
compliant material with its own 24 pairs per method, its own ledger and its own holdout; the two campaigns
then produce two candidates per method and cannot be merged into one "tuned method".

### 2.2 The cell and its geometry [S]

`sustained_release_oblique`, full-cycle timeline: 2.5 N along (outward + reference tangent)/√2 at the
true contact point, i.e. a 1.77 N outward pull plus a 1.77 N push along the direction of travel; raised-cosine
rise 20.0 to 20.5 s, hold to 30.5 s, cosine release to 35.5 s, then 27.3 s of post-release PATH. With the shared
Kp = 120 N/m the quasi-static tangential yield is 14.7 mm; the normal component changes penetration by 0.22 mm
on the stiff material (0.88 mm on the compliant one). This scenario has never been run in any full-cycle
study; the six other disturbed scenarios all remain for holdout. The matched nominal for every unit is the
same method's nominal at the same triple, so each unit is exactly two trials.

Why this cell rather than the pulse: it exercises the force loop (outward pull, wrist-sum re-press) and the
path loop (tangential yield and Kp-driven return) at once, it contains the release ramp that the task's
"yield then recover" framing is about, and it leaves both pure directions and all three pulses unseen for
transfer. The pulse's discriminating quantity in development was the rebound damping of a 1.0 to 1.6 Hz
closed-loop mode whose frequency lives in the assumed servo and contact model [P]; tuning three laws to damp
that mode would optimise a simulator property. The pulse belongs in holdout, where a candidate tuned on the
slow release must still handle it.

### 2.3 Limitations of the single-cell design, stated before the first unit

1. The objective's absolute level will be dominated by common-module floors. In the NO-v3 normal hold the
   hold-phase load term is 64.9 to 66.2 s of J for all three laws (the re-press against a 2.5 N pull that the
   wrist sum cannot distinguish from a load change, round 5 section 8); in the tangent hold the post-release
   path term is 30.2 to 31.7 s for all three [D]. On the oblique cell both floors will be present. EI is
   unaffected by a constant offset, but the report must decompose J so that the law-dependent part is visible.
2. Selection at one amplitude (2.5 N), one material, one surface, one prior, one onset time (20 s, protocol
   constant). Generalisation is a holdout question, never an inference from the training cell.
3. Under the same observer, the tangent push leaves a persistent attitude error that no law removes (section
   3.3); the oblique push will likely do the same. The attitude term of J will therefore carry a large
   law-independent part; it is kept because a law can still make it worse.
4. The cell has no development trace, so the first eight units are also the first look at its numerical
   behaviour; the step checks come only after unit 24 (section 4).
5. The low-load interaction seen for SFC under NO-v3 with a 2.5 N normal pull (0.843 N minimum, 0.434 s
   below 1 N [D]) may or may not occur with the 1.77 N normal component; if the untuned SFC seed is infeasible
   under the guards on this cell, that is reported as a result, and the guard is not relaxed.
6. The three FT-v1 seeds are at m = 4; if all incumbents stay on that bound the comparison has not explored
   mass at all and must say so.

What the training result can claim: for each method, the best feasible triple under identical modules,
bands, schedule and numerical setting on this cell, with the cross-method J difference declared resolved or
unresolved by section 4's criterion. What it cannot claim: that either proposal is better than SFC on the
task, that memory helps or hurts (that needs the identity arm in holdout), or anything physical.

### 2.4 The four repeat units

[S][R] Fresh-instance replays of full receipts reproduce all 31,916 records with zero mismatch in FP-v1,
NO-v3, FM-v1 and OT-v1; the runner has no stochastic element. The proposer's four repeat rows will
therefore be byte-identical pairs. Run them as the proposer proposes, verify that the four receipts'
`rows` hash equal, and report "4 deterministic repeats, zero variance, no confidence interval". This is
the honest content of the block in an offline campaign; it is not wasted, because it is the campaign's own
determinism evidence. Do not relabel refined-step runs, other scenarios or perturbed initial conditions as
repeats; if main prefers to amend the protocol so that the four units mean something else, that is an
amendment to record before launch, not a reinterpretation.

## 3. Q2: feasibility bands, the objective, and the common-module tradeoff

### 3.1 The tradeoff that the bands lock in [D]

Whole-PATH nominal descriptors, same laws and seeds, two observers:

| Method / observer | Load MAE N (after 20 s) | Path RMS mm | Attitude RMS deg (rad) | Normal-estimate RMS deg | Progress | Peak N | Saturation s | QP scaling s |
|---|---|---:|---|---:|---:|---:|---:|---:|
| SFC / NO-v3 | 0.112 (0.0066) | 5.433 | 2.560 (0.0447) | 2.56 | 0.855 | 6.223 | 0.00 | 0.24 |
| DSFC / NO-v3 | 0.071 (0.0052) | 5.219 | 1.162 (0.0203) | 1.16 | 0.875 | 5.886 | 0.00 | 0.16 |
| MSFC g50 / NO-v3 | 0.061 (0.0054) | 5.217 | 1.306 (0.0228) | 1.30 | 0.873 | 5.726 | 0.00 | 0.00 |
| SFC / legacy | 0.073 (0.0545) | 1.470 | 6.485 (0.1132) | 6.71 | 1.023 | 5.444 | 1.59 | 14.09 |
| DSFC / legacy | 0.067 (0.0480) | 1.340 | 6.494 (0.1133) | 6.71 | 1.022 | 5.546 | 2.36 | 14.41 |
| MSFC g50 / legacy | 0.062 (0.0419) | 1.162 | 6.557 (0.1144) | 6.76 | 1.022 | 5.762 | 0.89 | 8.23 |

NO-v3 versus legacy, same law: path RMS ×3.7 to ×4.5 worse, attitude ×2.5 to ×5.6 better, progress ×0.84 to
×0.86, steady-state load MAE (after 20 s) ×8 to ×10 better, QP task scaling from 8 to 14 s per cycle down to
0 to 0.24 s. The legacy observer's 1.2 to 1.5 mm is not "precise contact" either: it comes with progress
above one and a quarter of the cycle under QP scaling. The whole-PATH MAE ordering between laws (0.112 /
0.071 / 0.061) is the first-20-s entry transient (0.34 / 0.21 / 0.18 N); after 20 s the three laws regulate
to 0.005 to 0.007 N, as round 5 found under the frozen observer.

Judgment, not guarantee: the fair comparison will be run at about 5.3 mm path RMS and 0.86 progress because
that is what the frozen NO-v3 version delivers with any of the three laws. A contribution claim about
"precise" contact would require a common-module result that no arm of this campaign can produce; the
comparison should be reported as a comparison of yielding and recovery at the common module's precision
level, with the table above beside it. Using the frozen prior to show 1.36 deg attitude is not an option
(it fails the prior and curvature cases [R]); relaxing the nominal bands toward legacy numbers is not
an option either, because no NO-v3 arm can meet them.

### 3.2 Feasibility, declared now, identical for all arms

Nominal trial at the training cell (2 ms/8 substeps): a candidate is feasible only if it is no worse than
the untuned baseline seed on every descriptor, within the largest nominal step-resolution already observed
under NO-v3 [D][R] (path 0.035 mm, attitude 0.245 deg, progress 0.0035, MAE 0.0137 N from the across-prior
refinement; peak 0.044 N from the frozen nominal pair), rounded up:

| Descriptor | Baseline seed value | Margin | Band |
|---|---:|---:|---|
| Path RMS | 5.433 mm | +0.05 | ≤ 5.48 mm |
| Progress ratio | 0.855 | −0.005 | ≥ 0.850 |
| Attitude RMS | 2.560 deg | +0.25 | ≤ 2.81 deg (0.0490 rad) |
| Load MAE, whole PATH | 0.112 N | +0.015 | ≤ 0.127 N |
| Contact peak | 6.223 N | +0.05 | ≤ 6.27 N |
| Minimum load | 3.84 N | existing 1 N threshold | no sample below 1 N |
| Completion | | | full cycle, no failure, no source change |

Disturbed trial guards: no failure; true load never below 1 N (the existing `contact_loss_duration_s`
threshold) and never above the existing 8 N diagnostic limit; progress ratio ≥ 0.850 (the six NO-v3 holds
had 0.880 to 0.900 [D]). Saturation and QP seconds are reported, not gated.

Every band is set by the SFC seed, so "no worse than the baseline seed" and "worst-seed envelope" coincide.
The proposer takes one boolean; pass it as pair feasibility (nominal bands and disturbed guards together)
and record in the ledger that the field named `nominal_feasible` carries the pair gate, or add a second
field. Infeasible completed pairs are excluded from the GP by the proposer as written; they still consume
an ordinal. Stop rule: if a method has fewer than three feasible pairs after the eight initial units, stop
that method before EI and report, rather than letting EI extrapolate from one or two points.

### 3.3 The objective

For a completed feasible pair with the same method, material, preparation, dt and identity (the existing
`compare_pair` matching rule), on the PATH clock with onset 20.0 s, release 35.5 s and T = 62.83 s:

```
J_load = (1 / 0.5 N)   ∫_{20.0}^{T} | load_d(t) − load_n(t) | dt
J_path = (1 / 2 mm)    ∫_{35.5}^{T} ‖ x_d(t) − x_n(t) ‖ dt
J_att  = (1 / 0.05 rad) ∫_{20.0}^{T} | θ_d(t) − θ_n(t) | dt
J      = J_load + J_path + J_att          [seconds]
```

load is the evaluator's true normal load, x the TCP position, θ the axis-angle orientation error against the
true inward normal; subscripts d and n are the disturbed and matched nominal trials. Path deviation during
the hold is the intended yielding and is excluded; load and attitude deviation during the hold are not
intended and are included. The two length/force bands are the existing recovery bands, so J is measured in
"band-seconds" and a trajectory that sits at a band edge for one second contributes one second. The
attitude band is the frozen NO-v3 observer's rate cap (0.05 rad/s) times one second, i.e. the error this
observer version can remove in a second; it is a constant of the frozen module, not a fitted number. J is
nonnegative and smooth in the trajectories, so it is compatible with the proposer's contract; it has no
crossing time, so a 0.1 N shift of a band changes it by a factor, not by a factor 2.4 as it did the SFC
pulse recovery in round 5.

Behaviour on every existing full-cycle pair [D]; the 2 mm/0.5 N recovery is reproduced exactly beside it:

| Pair | Arm | J_load | J_path | J_att | J | Recovery s | Peak N | Min N |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| NO-v3 normal hold | SFC | 76.97 | 23.80 | 22.38 | 123.2 | 20.384 | 6.14 | 0.84 |
| | DSFC | 71.42 | 9.46 | 9.64 | 90.5 | 3.834 | 5.92 | 1.13 |
| | MSFC | 68.35 | 5.41 | 9.34 | 83.1 | 1.122 | 5.65 | 1.34 |
| NO-v3 tangent hold | SFC | 13.56 | 31.67 | 52.47 | 97.7 | 8.848 | 6.82 | 3.70 |
| | DSFC | 7.34 | 30.20 | 56.33 | 93.9 | 8.424 | 5.54 | 4.22 |
| | MSFC | 6.10 | 30.34 | 55.43 | 91.9 | 8.630 | 5.38 | 4.42 |
| frozen tangent hold | SFC | 11.04 | 2.74 | 2.81 | 16.6 | 1.004 | 6.23 | 3.84 |
| | DSFC | 2.41 | 2.48 | 2.80 | 7.7 | 0.942 | 5.55 | 4.22 |
| | MSFC | 1.65 | 2.52 | 2.77 | 6.9 | 1.008 | 5.38 | 4.42 |
| | MSFC-identity | 1.58 | 2.52 | 2.77 | 6.9 | 1.008 | 5.36 | 4.43 |
| frozen pulse, 2 ms | SFC | 17.69 | 5.53 | 0.10 | 23.3 | 4.526 | 8.02 | 1.24 |
| | DSFC | 14.11 | 4.86 | 0.08 | 19.1 | 4.154 | 8.23 | 1.23 |
| | MSFC | 8.81 | 4.01 | 0.05 | 12.9 | 2.352 | 7.83 | 1.34 |
| | MSFC-identity | 8.28 | 3.81 | 0.05 | 12.1 | 2.300 | 7.68 | 1.37 |
| frozen pulse, 1 ms | MSFC | 9.65 | 4.08 | 0.05 | 13.8 | 2.629 | 7.85 | 1.33 |
| | MSFC-identity | 9.05 | 3.87 | 0.05 | 13.0 | 2.313 | 7.70 | 1.36 |

Reading. (i) Where recovery is force-bound (pulse) J and recovery order the arms the same way; where
recovery is path-bound and observer-dominated (tangent holds, all within 0.07 s frozen and 0.4 s under
NO-v3) recovery ranks DSFC first while J ranks by the hold-phase load regulation, which is the quantity the
laws actually differ in there (round 5 section 5). (ii) Under twelve weightings, F ∈ {0.3, 0.4, 0.5, 1.0} N
× X ∈ {1, 2, 3} mm, the ordering is identical in all five pair groups (`objective_band_weight_sweep`). (iii)
The attitude weight matters once: at half the attitude band (0.025 rad) SFC overtakes DSFC in the NO-v3
tangent hold, because DSFC's attitude excursion is the largest (56.3 versus 52.5 s) while its load regulation
is better; at 0.05 and 0.1 rad the order is MSFC, DSFC, SFC. This is the weight-sensitivity that a scalar
objective must own, and it is the reason the bands are frozen here rather than after the first units.
(iv) The MSFC-first ordering in these development cells is a seed-parameter statement about stiffer-damped
coefficients (round 5 section 7), not a prediction for the tuned comparison.

The attitude finding behind (iii) [D]: after the tangent hold under NO-v3 the post-release attitude RMS is
4.62 / 4.65 / 4.56 deg for SFC / DSFC / MSFC against 0.73 deg in the nominals, with maximum deviations of
9.8 deg, and it does not return within the 27 s left in the cycle; after the normal hold it returns to 0.73
to 0.76 deg for DSFC and MSFC but stays at 1.94 deg for SFC. A hard post-release attitude guard at the
nominal band would therefore exclude every candidate on any cell with a tangential push, which is why
attitude is inside J and not a gate. That the excursion is nearly identical across laws is a common-module
statement about the observer under a sustained tangential force, consistent with round 4's contamination
finding, and it must appear in the final report whatever the laws do.

Selection and ties: within a method the proposer selects the minimum J among feasible pairs (ties by
candidate key, as coded). Across methods no winner is named; the report gives J and its three components,
peak, minimum load, recovery, saturation and QP seconds for each incumbent, and the section 4 criterion
decides "resolved" or "unresolved". No Pareto front is needed at 24 points; the decomposition is the
Pareto information. A pre-declared secondary order, used only for wording when J is unresolved: lower
contact peak, then lower disturbed attitude RMS.

Disclosure: J was designed after seeing the normal-hold, tangent-hold and pulse development pairs above; it
has not seen any oblique-sustained trajectory. Its constants come from the metric module and the frozen
observer, not from fitting; the table is a demonstration, not a pre-registration on those cells.

## 4. Q3: numerical checks and resolution-aware benefit criteria

### 4.1 Two separated checks, after the freeze, never as feedback

For each method's frozen incumbent, at the training cell: check A, controller 1 ms with plant step held at
0.25 ms (4 substeps); check B, controller 2 ms with plant step 0.125 ms (16 substeps); each with its own
matched nominal at the same setting, so four trials per incumbent, twelve in total, about 0.6 worker-hours
at the 190 s per 1 ms trial observed in the FP-v1 and NO-v3 refinements (`cost_from_manifests`). The
FP-v1 refinement halved both clocks at once and cannot separate them; DC-v1's 2×2 on the SFC normal hold
showed controller-step effects (0.50 s of low-load duration, 0.12 N of peak) an order of magnitude larger
than plant-step effects (0.05 s, 0.01 N) [R]. Rules: the incumbent is frozen at the end of unit 20 (the
discovery block); the checks run after unit 24; their outcome cannot change the incumbent or trigger a
re-selection. If an incumbent fails qualification the method's result is "incumbent not numerically
qualified", not the runner-up. A pre-declared single fallback ("check the second-best feasible discovery
row") is admissible only if written into the contract before unit 1 and counted as validation cost; I do
not recommend it.

### 4.2 Qualification bound, declared now, with disclosure

Bound: over the full PATH, the pointwise |load difference| between the base run and each refined run is
≤ 0.5 N and the pointwise TCP difference is ≤ 2 mm, for both members of the pair. These are the existing
recovery bands: if refinement moves a trajectory by more than the band that defines recovery, the recovery
classification on that pair is not numerically meaningful. A pair that fails the bound keeps all its
descriptors in the report but carries no recovery-time statement. Disclosure: I have seen the step
differences already reported, and the bound sorts them as follows (`band_rule_disclosure`):

| Case | Max force diff N | Max TCP diff mm | Within 0.5 N / 2 mm |
|---|---:|---:|---|
| FP-v1 MSFC g50 pulse, both clocks halved | 0.126 | 0.033 | yes |
| FP-v1 MSFC-identity pulse, both halved | 0.123 | 0.031 | yes |
| NO-v3 DSFC across-prior nominal, both halved | 0.195 | 0.442 | yes |
| OT-v1/DC-v1 SFC NO-v3 normal hold, both halved | 1.005 | 1.002 | no |
| GM-v1 MSFC g100 normal hold, plant step halved | 3.603 | 0.672 | no |
| GM-v1 MSFC g50 normal hold, plant step halved | 0.044 | 0.012 | yes |
| common-fix SFC tangent hold, plant step halved | 0.036 | 0.017 | yes |

The bound is not chosen to pass a number; it is the pre-existing band, and it rejects the two cases the
retained reports already call unsettled (OT-v1's 1 ms check, GM-v1 g100). Main may set a tighter bound
before launch; it must not set a looser one after seeing the incumbents' checks.

### 4.3 Resolution-aware benefit criterion

Because every incumbent is run at the base setting and both refined settings, the natural criterion is
paired: for a descriptor d and two arms A and B, the difference d_A − d_B is computed at each of the three
settings; the benefit is resolved if the sign is the same at all three and the smallest |d_A − d_B| exceeds
the range of d_A − d_B across settings. The conservative unpaired form (|d_A − d_B| at base greater than
r_A + r_B, with r the per-arm change under refinement) is reported beside it. On the only pair that already
has both arms at two settings [D] (FP-v1 refinement, both clocks halved together):

| Descriptor | on − identity, 2 ms | on − identity, 1 ms | Range | Paired | Unpaired (r_on + r_id) |
|---|---:|---:|---:|---|---|
| J | +0.736 | +0.815 | 0.078 | resolved | not (0.91 + 0.83) |
| J_load | +0.532 | +0.602 | 0.070 | resolved | not |
| J_path | +0.201 | +0.209 | 0.007 | resolved | resolved (0.07 + 0.06) |
| Peak load N | +0.147 | +0.150 | 0.003 | resolved | resolved (0.014 + 0.011) |
| Recovery s | +0.052 | +0.316 | 0.264 | not resolved | not (0.28 + 0.01) |

The two forms disagree on J because refinement shifts J_load by +0.8 s for both arms in the same direction
(the pulse cell's 0.12 N pointwise drift integrated over 42 s); the paired form sees that the drift is
common, the unpaired form does not. The paired form is the criterion to pre-register; the unpaired form
is the conservative bound to print with it. Recovery time is unresolved under both, as round 5 said.

Descriptors carried through the criterion: J and its three components, contact peak, minimum load,
recovery time (only when 4.2 qualifies both arms), the fixed-0.5 s envelope stay-below times as
supplementary, yield peak, residual, saturation and QP seconds.

### 4.4 Offline verification versus physical repeats

The simulator is deterministic and has no identified noise model; inventing one is excluded. So: every
offline cell is one run; the only error bar is the numerical resolution from 4.1 to 4.3; there is no
confidence interval, no p-value, and no "5 repeats" offline. The protocol's `holdout_repeats = 5` is a
hardware clause and remains one. Report the offline holdout as "deterministic, n = 1 per cell; differences
smaller than the paired resolution are unresolved". What must wait for the physical pilot [P]: any
variability or repeatability statement; sensor bias, stick-slip, real servo and contact dynamics and the
frequency and damping of the 1.0 to 1.6 Hz rebound mode; wrist versus contact force separation under a
real hand; 500 Hz timing (compute-budget-v1: repaired PATH p95 0.73 ms with unexplained 22 to 26 ms outliers,
not realtime-qualified [R]); the resident program's ownership and contact state (preflight-v2 [R]); and the
canonical construction blocked by the RNN xacro digest (provider-admission-v2 [R]). None of these is
changed by any offline result.

## 5. Q4: a finite pre-registered holdout

### 5.1 Blocks

All cells: NO-v3 observer at the OT-v1 parameters, 2 ms/8 substeps, Kp 120, Kz 0, full cycle, incumbents
frozen from section 2, the runner's existing `estimator_parameters`, `surface_parameters` and
`preparation` inputs; no runtime change. Arms: SFC, DSFC and MSFC incumbents; MSFC-identity (MSFC's
incumbent triple with λ_min = 1) wherever a memory statement is to be made; SFC_RADIAL (SFC's incumbent
triple, radial form) wherever the baseline's per-axis geometry is to be separated from its coefficients.
Nominal trials are shared per (arm, material, surface, prior, preparation) and reused by hash.

| Block | Material | Surface (κxx, κyy)/m | Prior | Preparation | Disturbed scenarios | Arms | Disturbed + new nominals | Status of the conditions |
|---|---|---|---|---|---|---:|---:|---|
| H1 direction/shape transfer | stiff_low_mu | mild (0.8, 0.4) | approach | cold | sustained normal, sustained tangent, pulse normal, pulse tangent, pulse oblique | 5 | 25 + 2 = 27 | normal/tangent holds and pulse oblique viewed with seeds; pulse normal/tangent never run |
| H2 material transfer | compliant_high_mu | mild | approach | cold | all six | 5 | 30 + 5 = 35 | NO-v3 never run on this material; legacy and frozen were (SE-v1, TR-v1) |
| H3 unknown surface | stiff_low_mu | strong (6, 8) | approach | cold | sustained oblique, pulse oblique | 5 | 10 + 5 = 15 | only the DSFC seed nominal exists under NO-v3 (progress 0.745, path 7.0 mm, attitude 6.0 deg [D]) |
| H4 prior uncertainty | stiff_low_mu | mild | along 10 deg, across 10 deg | cold | sustained oblique | 5 | 10 + 10 = 20 | only DSFC seed nominals exist (across: 5.37 mm, 2.69 deg, 0.857 [D]) |
| H5 warm preparation | stiff_low_mu | mild | approach | warm (2 s baseline) | sustained oblique, pulse oblique | 5 | 10 + 5 = 15 | flag exists in the runner, never used in a full-cycle study; matched nominal must be warm too |

Total 112 trials, about 2.5 worker-hours at the 79 s per 2 ms trial observed in the retained manifests.
H1 is not an independent test of the research programme's choices (those cells were seen), but it is an
independent test of the tuned triples, which never saw them; label it so. H2 to H5 are unseen for SFC and
MSFC under NO-v3 and, apart from the DSFC seed nominals, unseen for DSFC. Choosing new factor values does
not validate the simulator's physics [P]; the curvature field, Coulomb model and PI servo are the same
assumptions in every block.

### 5.2 Cold and warm

[S] MSFC's memory is two first-order low-pass states with τ_force = 0.192 s and τ_recovery = 0.202 s; the
structure relaxes to 5 percent within 0.6 s of any excitation and the metric sits at its nominal steady
state (eigenvalue 0.97) long before the 20 s onset. There is no long-horizon memory whose cold or warm state
could be prepared across cycles; the only state-preparation factor that exists is the runner's
`preparation` flag, where warm inserts a 2 s stationary 5 N baseline before the 1 s entry and cold does not.
H5 tests that factor for all arms with matched warm nominals. If main wants a genuinely warm memory at
intervention, the only construction is a second intervention inside one second of the first, which needs a
new scenario in the protocol and is out of scope here; it should be listed as untested.

### 5.3 Rules

- Freeze the block table, the arms and the objective constants before the first holdout trial; anything
  added afterwards is labelled post-hoc exploratory and carries no claim.
- A failed or shortened trial is retained and reported; it is not re-run with changed parameters. A cell
  that fails for all arms is a common-module outcome; a cell that fails for one arm is a negative result for
  that arm.
- Claim-conditional checks: any cell on which a cross-arm benefit is stated needs section 4.1's two checks
  for both compared arms (4 trials per arm per cell) and passes 4.2 and 4.3; cells without checks carry
  descriptive numbers only. Upper bound if one representative cell per block is checked for the three tuned
  arms: 60 trials, about 3.2 worker-hours.
- Memory statements need the identity arm on the same cell; geometry statements need the radial arm.
- No holdout number feeds any parameter, band or bound.

### 5.4 Cost summary (worker time, from retained manifests; not realtime evidence)

| Stage | Trials | Worker-hours |
|---|---:|---:|
| Training, 3 methods × 24 pairs | 144 | 3.2 (wall ≈ 0.5 h with six workers, since EI units are sequential per method) |
| Training-cell checks, 3 incumbents × 2 checks × 1 pair | 12 | 0.6 |
| Optional ablation pairs at the training cell (identity, radial) | 4 | 0.1 |
| Holdout, five blocks | 112 | 2.5 |
| Claim-conditional checks, upper bound | 60 | 3.2 |
| Total upper bound | 332 | about 9.6 |

Engineering not counted: the campaign runner that seals pairs, computes J and feasibility, and keeps the
ledger; the holdout runner over the existing factor inputs; hash reuse of shared nominals.

### 5.5 Missing assumptions and open risks

- NO-v3 has never been run on the compliant material or with SFC and MSFC on the prior and strong-surface
  cells; H2 to H4 may fail or be infeasible for every arm, which is a result about the module, not a ranking.
- The proposer excludes infeasible completed pairs from the GP; near a feasibility boundary EI can spend
  units re-proposing infeasible regions. The section 3.2 stop rule bounds the damage; it does not remove it.
- All seeds are on the m = 4 bound; a boundary incumbent must be reported as such.
- The friction-bound residual of round 5 needs a declared mu-hat before it can be a diagnostic column; it
  is not part of feasibility or J.
- No sensor noise, no stick-slip, no orientation-dependent contact patch, no identified servo; every J value
  is a property of this model [P].
- The sustained-oblique cell's numerical behaviour is unknown until the checks in 4.1 run; if all three
  incumbents fail 4.2 on it, the campaign has produced tuned triples with no qualified recovery statement,
  and the report must say exactly that.

## 6. Negative and unresolved evidence preserved

- Round 5: memory-on minus identity +0.147/+0.150 N peak at both steps; recovery-time gap 0.05 to 0.32 s
  and unresolved; twelve matched memory pairs inert or adverse; the −1.01 s compliant pulse [R] unresolved
  and still unrefined; frozen prior fails prior and curvature; SFC pulse recovery factor 2.4 under a 0.1 N
  band change.
- OT-v1/DC-v1: SFC under NO-v3 normal hold 0.843 N minimum, 0.434 s below 1 N, 20.4 s recovery, 1.005 N
  step sensitivity, not numerically settled; NO-v3 recovers 8.4 to 8.8 s after the tangent hold for all laws.
- GM-v1: g100 3.6 N plant-step sensitivity with and without memory; common-fix pilot advancement not passed.
- This round [D]: persistent post-release attitude error of 4.6 deg after a tangential push under NO-v3
  for all three laws; hold-phase load floor of about 65 s of J under a 2.5 N normal pull for all laws; SFC's
  post-release attitude 1.94 deg versus 0.73 deg after the normal hold; the SFC/DSFC ordering in the tangent
  hold depends on the attitude weight.
- Advancement: formal units consumed 0; no candidate is promoted; nothing here authorises motion [P].

## 7. What this round does not establish

- No tuned candidate exists; every number above is a seed-parameter development number used to design
  and demonstrate the contract.
- J's landscape over (m, mu, g) on the oblique cell is unknown; the GP's fixed lengthscale and noise-free
  assumption are main's proposer choices, taken as given.
- The attitude band (0.05 rad) is a constant of the frozen observer version; a different observer version
  would need its own band and its own campaign.
- The qualification bound (0.5 N / 2 mm) is declared after seeing seven step differences; the disclosure is
  in 4.2 and the decision to tighten is main's before launch.
- Nothing here validates the plant, and no offline outcome transfers numerically to the robot [P].

## Reproduction

```bash
# from the experiment root; caches under /tmp (not retained)
D=report/yield-fair-tuning-v1/discussion; P=.venv-contact-six/bin/python; mkdir -p /tmp/yfp6
x() { $P $D/scripts/extract_rows_v6.py "$2" /tmp/yfp6/"$1".npz; }
x no3-nom-SFC  runs/yield-observer-transfer-v1/SFC-nominal.json.gz
x no3-nrm-SFC  runs/yield-observer-transfer-v1/SFC-sustained_release_normal.json.gz
x no3-tan-SFC  runs/yield-observer-transfer-v1/SFC-sustained_release_tangent.json.gz
x no3-nom-DSFC runs/yield-normal-v3/combined-mild-approach.json.gz
x no3-nrm-DSFC runs/yield-normal-v3/combined-normal-hold.json.gz
x no3-tan-DSFC runs/yield-normal-v3/combined-tangent-hold.json.gz
x no3-nom-MSFC runs/yield-observer-transfer-v1/MSFC-nominal.json.gz
x no3-nrm-MSFC runs/yield-observer-transfer-v1/MSFC-sustained_release_normal.json.gz
x no3-tan-MSFC runs/yield-observer-transfer-v1/MSFC-sustained_release_tangent.json.gz
x leg-nom-SFC  runs/yield-offset-ablation/SFC-nominal.json.gz
x leg-nom-DSFC runs/yield-offset-ablation/DSFC-nominal.json.gz
x leg-nom-MSFC runs/yield-gain-memory-v1/MSFC-GM-v1-g50-on-nominal-plant8.json.gz
x no3-along-DSFC  runs/yield-normal-v3/combined-mild-along.json.gz
x no3-across-DSFC runs/yield-normal-v3/combined-mild-across.json.gz
x no3-strong-DSFC runs/yield-normal-v3/combined-strong-approach.json.gz
# round-5 cache /tmp/yfp5: see report/yield-frozen-pulse-v1/discussion/fable-round5.md, Reproduction
$P $D/scripts/analyze_round6.py > $D/data/round6-analysis.json
$P $D/scripts/round6_receipt.py > $D/round6-input-receipt.json
```
