# Round 8 advisory: the first four shared triples, the SFC entry gate, the SFC-02 tradeoff, and the memory end-state

Role: advisory, non-blocking, no live authority; main owns integration, scientific acceptance and hardware.
First of at most two iterations this round. HEAD was b0059632 in the session's opening git snapshot and 4cd78c5c
(main's commit freezing the round-8 input snapshot) at the first read-only check and at receipt time. Written only
under `report/yield-fair-training-v1/discussion-round8/`. No run, no native law stepped, no ledger or SQLite opened,
no edits elsewhere, no Git writes, no subagents, no devices, no network. The active Grok validation-writer worktree
was not opened and its draft is not audited. Training was not touched; nothing here proposes a mid-campaign change.

Inputs: the fixed snapshot `round8-input-snapshot.json` (25 sealed attempts at 2026-09-20 04:04 UTC) and exactly the
25 raw artifacts it lists, each re-hashed against its snapshot digest before parsing (25 of 25 match). Three further
attempt directories present on disk (`DSFC-04-nominal`, `DSFC-04-disturbed`, `SFC-04-disturbed`) were not opened.
The campaign, tuner and observer configs and the frozen tools were read only and hash-match the launch binding
(all 22 bound files, the native library, the QP library, the recomputed campaign-protocol digest). Round 7 and its
adjudication, validation reservation v2 and the runner-review counterexample were read as reports. Derived numbers
are in `data/round8-analysis.json` and `data/round8-mechanics.json` from the two scripts under `scripts/`.

Post-snapshot note, from read-only `git log` titles at receipt time only: main committed e56abf11 (validation
execution integrated with the training freeze), aa1a42bb ("Record SFC initial feasibility stop without relaxing
campaign"), a0fc6d61 and 7e1c7231 after the snapshot, and an untracked `report/yield-round8-main-review-v1/`
appeared. None of these, and none of the 26 attempt directories now on disk beyond the 25 listed, were opened. The
analysis below is on the fixed snapshot as instructed; if the SFC stop is what its commit title says, section 2.5
path B is the live branch and the entry-gate arithmetic in 2.3 is superseded by main's record.

Evidence classes: [S] structural (code, protocol, algebra); [D] re-derived here from the 25 verified receipts;
[R] reported in hashed prior reports, not re-derived; [H] hypothesis; [P] physical, not established by any
simulator result.

## 0. Answers

1. **Q1.** The five out-of-band nominals are not concentrated on the progress/path/attitude tradeoff. Band
   violations across them count load peak 4, path 2, attitude 2, load MAE 2, progress 1 [D]. Only SFC-02 is a pure
   path/attitude/progress failure; SFC-03 mixes it with load; DSFC-03, MSFC-03 and SFC-04 are load-only. The
   attitude band is in practice an observer-error band: whole-path attitude RMS equals normal-estimate RMS to
   0.08 to 0.14 deg in all 13 nominal members [D]. SFC sits at 0.91 to 1.005 of every band at its own seed while
   the proposals sit at 0.37 to 0.97, so any triple that moves an SFC descriptor by one percent fails it [D]. The
   gate arithmetic: SFC has 2 pair-feasible of 5 decided initial units and needs 1 of units 5, 6, 7; DSFC and MSFC
   already have 3 [D]. If SFC ends below 3 of 8, the frozen runner stops it before EI and the frozen freeze
   requires 24 pairs per method, so no freeze and hence no validation is possible without a versioned
   supersession [S]. Three bounded paths are given in 2.5; the one I recommend declaring now is a reduced-budget
   SFC incumbent defined by the already-frozen min-J rule over its 8 initial units, labelled as such in every
   table, with the timing disclosed (prospective for units 5 to 7, not for units 0 to 4).
2. **Q2.** At the shared triples (not the methods' own seeds), the sign-stable differences at 4 of 4 are all
   nominal-precision and load-margin descriptors: the proposals have lower nominal path RMS, attitude, load MAE and
   peak, higher nominal progress and load minimum, a higher and earlier disturbed load minimum (+0.49 to +0.78 N),
   higher disturbed progress, less saturation, and a larger post-release overshoot [D]. J, its three components,
   the disturbed attitude peak, the post-release attitude RMS and the recovery time favour SFC at the three m = 4
   triples and flip at the m = 13.2, mu = 43 triple where all arms are infeasible [D]. So the J ordering is
   parameter-dependent and the precision/margin orderings are not, on this evidence. MSFC minus DSFC at a shared
   triple is the memory ablation at fixed coefficients [R][S]: J differs by −0.55, −0.01, +0.15, +0.79 band-s,
   sign-unstable and below one percent of J; the sign-stable part is a tiny nominal-precision and saturation
   penalty for the active metric [D]. No benefit claim, no CI, no resolution claim before the post-freeze checks.
3. **Q3.** SFC-02's low J with an infeasible nominal is a genuine tradeoff, not a misreport. Its nominal fails
   because the per-axis law at mu·g = 105 produces about twice the load oscillation of the radial laws in the
   3 to 10 s segment (MAE 0.63 to 0.72 N against 0.29 to 0.45 N), the motion-based observer converts that into a
   normal-estimate error that grows to 6.4 deg by 12 s and then freezes, because updates stop in every arm from
   12 s to 20.5 s as the path slows, and the frozen tilt costs path and progress [D]. Under the oblique hold SFC-02
   tilts less (peak 6.4 deg against 8.7), overshoots less after release (2.09 mm against 2.88) and recovers sooner
   (2.15 s against 2.99 s), which is where the 13.2 band-s of J come from (Jatt −9.4, Jpath −2.2, Jload −1.5);
   it also drops the load deeper and later (1.49 N at 22.4 s against 2.26 N at 20.6 s) [D]. All 25 flags and all 12
   objectives recompute exactly [D]. Two semantic weaknesses are real but declared computations, not misreports:
   the disturbed progress guard is inflated by the oblique push and passes every SFC pair only because of it; and
   the disturbed load-peak guard is inert on this cell because every load peak occurs before 20 s [D].
4. **Q4.** If the active metric remains adverse after fair tuning and the identity ablation, the only change
   justified by mechanism is subtractive: a versioned configuration that fixes the metric at identity, which the
   ablation itself tests and which adds no parameter, cell or controller. If the ablation is unresolved on every
   cell, the end-state is a reported null with no design change. A memory-timescale change is admissible only as
   a new pre-registered development study with a stated engagement hypothesis, and only on cells not used to
   choose it. Rule: a change is mechanism-justified when it is derived from a receipt-measurable state quantity
   (engagement window, eigenvalue floor, timescale ratio) that predicts the sign of a descriptor change before the
   run; it is overfitting when it is chosen because it lowers J on viewed cells. RNN stays optional, the platform
   task fixed, no portfolio, no new validation cells, no intention or whole-arm safety claim [P].

## 1. Inputs, verification and the contract as applied

- All 25 artifact digests match the snapshot; all 25 sets of nominal or disturbed checks recomputed from rows with
  the frozen evaluator's rules match the snapshot flags; the 12 objectives recomputed with the frozen
  `_objective_components` match the snapshot to 0.0; every artifact's reported metrics equal `summarize_trial`
  on its rows [D].
- Contract as bound: nominal bands path RMS ≤ 5.48 mm, progress ≥ 0.85, attitude RMS ≤ 2.81 deg, load MAE ≤ 0.127 N,
  load peak ≤ 6.27 N, load min ≥ 1 N; disturbed guards load min ≥ 1 N, peak ≤ 8 N, progress ≥ 0.85; J = Jload
  (from 20 s, 0.5 N) + Jpath (from 35.5 s, 2 mm) + Jatt (from 20 s, 0.05 rad), left sample-hold to the exact period
  62.83 s [S].
- Typed objective versus selection: `objective_eligible` is true for all 12 complete pairs including the four with
  an out-of-band nominal or a failed guard; `pair_feasible` is true for 8 of 12 and is what the proposer sees
  through the ledger adapter [S][D]. This distinction is applied throughout.

## 2. Q1: the first four shared triples and the SFC entry gate

### 2.1 Where the bands bite [D]

Nominal members, value and ratio to its band (ratio > 1 fails an upper band; < 1 fails the progress band):

| Member | path mm (/5.48) | progress (/0.85) | attitude deg (/2.81) | MAE N (/0.127) | peak N (/6.27) | min N | out of band |
|---|---:|---:|---:|---:|---:|---:|---|
| SFC-00 | 5.433 (0.991) | 0.8546 (1.005) | 2.560 (0.911) | 0.112 (0.881) | 6.223 (0.992) | 3.84 | none |
| DSFC-00 | 5.220 (0.953) | 0.8769 (1.032) | 1.043 (0.371) | 0.075 (0.593) | 6.060 (0.966) | 4.20 | none |
| MSFC-00 | 5.221 (0.953) | 0.8766 (1.031) | 1.060 (0.377) | 0.077 (0.605) | 6.073 (0.969) | 4.19 | none |
| SFC-01 | 5.473 (0.999) | 0.8525 (1.003) | 2.733 (0.972) | 0.097 (0.765) | 6.023 (0.961) | 4.04 | none |
| DSFC-01 | 5.219 (0.952) | 0.8751 (1.029) | 1.162 (0.414) | 0.072 (0.563) | 5.886 (0.939) | 4.27 | none |
| MSFC-01 | 5.219 (0.952) | 0.8750 (1.029) | 1.167 (0.415) | 0.072 (0.567) | 5.893 (0.940) | 4.27 | none |
| SFC-02 | 5.535 (1.010) | 0.8469 (0.996) | 3.231 (1.150) | 0.113 (0.891) | 6.142 (0.980) | 3.94 | path, progress, attitude |
| DSFC-02 | 5.216 (0.952) | 0.8735 (1.028) | 1.292 (0.460) | 0.060 (0.474) | 5.717 (0.912) | 4.22 | none |
| MSFC-02 | 5.217 (0.952) | 0.8733 (1.027) | 1.306 (0.465) | 0.061 (0.480) | 5.726 (0.913) | 4.21 | none |
| SFC-03 | 5.497 (1.003) | 0.8512 (1.001) | 2.835 (1.009) | 0.135 (1.066) | 6.474 (1.033) | 3.59 | path, attitude, MAE, peak |
| DSFC-03 | 5.467 (0.998) | 0.8522 (1.003) | 2.697 (0.960) | 0.126 (0.990) | 6.444 (1.028) | 3.73 | peak |
| MSFC-03 | 5.467 (0.998) | 0.8522 (1.003) | 2.698 (0.960) | 0.126 (0.990) | 6.444 (1.028) | 3.73 | peak |
| SFC-04 | 5.406 (0.987) | 0.8712 (1.025) | 1.548 (0.551) | 0.251 (1.977) | 6.806 (1.085) | 2.87 | MAE, peak |

Triples: 0 = SFC seed (4, 393, 0.0521); 1 = DSFC seed (4, 311, 0.0642); 2 = MSFC seed g50 (4, 1228, 0.0858);
3 = Sobol (13.20, 43.19, 0.1562); 4 = Sobol (5.32, 729.0, 0.0284) [S]. Reading by triple: unit 2 has mu·g = 105
against about 20 at the two other seeds, and only SFC fails, on the path/attitude/progress cluster. Unit 3 has the
lowest damping and the highest g, and all three laws overshoot the nominal load peak (6.44 to 6.47 N, spread
0.03 N); DSFC and MSFC are near-misses on every other band (path 0.998, MAE 0.990, progress 1.003). Unit 4 has the
lowest g/m sampled so far (0.0053 against 0.013 to 0.021 at the seeds) and SFC fails on load only (MAE at 1.98 of
the band), with attitude and path comfortable. So the failure cluster is set by the triple, not by one shared
tradeoff, and the load-peak band is the most frequently violated one.

Two structural facts about the bands. The attitude band is an observer band: whole-path attitude RMS equals the
normal-estimate RMS to 0.08 to 0.14 deg RMS for all 13 nominal members (orientation gain 4 s^-1 tracks the
estimate) [D]. And the peak band is decided in the early nominal transient: every member's load peak occurs
before 20 s (0.09 to 8.42 s), DSFC and MSFC in the entry transient at 0.09 to 0.29 s except unit 2 (8.42 s), SFC at
6.8 to 7.3 s except unit 3 (0.29 s) [D].

### 2.2 Why SFC fails bands the proposals pass, at the same coefficients [D]

The bands were placed at the SFC seed's values with step margins [R]. At its own seed SFC is at 0.991, 1.005,
0.911, 0.881 and 0.992 of the five upper/lower bands; DSFC and MSFC are at 0.95, 1.03, 0.37, 0.59 and 0.97. At all
four shared triples the proposals' nominal path RMS, attitude, MAE and peak are lower and their progress and load
minimum higher (section 3.2, sign-stable 4 of 4). So the same bands are objectively tighter for SFC because its
nominal precision on this cell is worse at every shared coefficient, and the gate is partly circular because the
bands were placed on SFC's own floor. Both sentences are needed. Neither licenses "SFC is harder to tune"; what is
licensed is "the per-axis law has worse nominal precision than the radial laws at the four shared coefficients on
this cell", which is the cross-law by-product round 7 said the campaign could produce, now with a sign at 4 of 4.
Round 7 main's correction stands: none of this attributes the 5.2 to 5.5 mm floor to law inaccessibility; the
floor is a module property [R], and the law differences here are 0.03 to 0.32 mm on top of it.

### 2.3 The entry gate: arithmetic, the frozen dead end and the remaining triples

- Counts [D]: SFC has units 0 and 1 pair-feasible, units 2, 3, 4 infeasible at the nominal; 2 of 5 decided; it
  needs at least 1 pair-feasible unit among 5, 6, 7. DSFC and MSFC have units 0, 1, 2 pair-feasible and unit 3
  infeasible; the gate is already met for both regardless of units 4 to 7.
- Frozen code path [S]: `run_next` stops a method at its first new unit at index 8 if fewer than 3 of the first 8
  are completed and pair-feasible ("stopping before EI without relaxing or refunding"). The other two methods
  continue independently to 24. `freeze_campaign` and `YieldContactLedger.freeze` both require exactly 24 pairs
  for every method, and the validation runner's `create` requires a digest-bound freeze export with all three
  incumbents [R]. Consequence: an SFC stop makes the freeze unreachable and the validation unlaunchable without a
  versioned supersession. The stop rule and the freeze rule were declared separately and never reconciled; this
  is a latent contract gap, not a runtime bug, and it needs a decision only on the branch where SFC stops.
- Remaining shared initial triples, deterministic from the frozen proposer (seed 20260920) [S]: unit 5 (m 7.64,
  mu 249.4, g 0.0771; g/m 0.0101, mu·g 19.2), unit 6 (m 11.02, mu 983.6, g 0.00934; g/m 0.00085), unit 7 (m 12.06,
  mu 186.1, g 0.0134; g/m 0.0011). Units 6 and 7 have g/m five to six times below unit 4, where SFC already failed
  load MAE at twice the band; unit 5 is the only one in the seeds' g/m range [H]. That is a description of the
  schedule, not a prediction to act on, and it is not an argument for trimming or reordering it.

### 2.4 Precise interpretations if SFC ends below 3 of 8 while the proposals advance

Each is a sentence the report could carry; none is a superiority claim.

- Band-placement reading [S][D]: the entry gate applies SFC-seed bands with about one percent margin to SFC and
  with 5 to 60 percent margin to the proposals; a low SFC feasibility rate is partly a property of where the bands
  sit. This is disclosed, not corrected, because the bands were declared before observation.
- Cross-law precision reading [D]: at 4 of 4 shared coefficients the per-axis law has worse nominal path,
  attitude, MAE and peak than the radial laws, with the largest gaps at high mu·g (unit 2) and vanishing gaps at
  low mu (unit 3). This is the substantive content behind the gate outcome and is reportable as a fixed-coefficient
  result on this cell.
- Design reading [S]: five of the eight initial triples are shared Sobol points far from the seed family; the
  initial design was chosen for coverage, not for SFC feasibility, and it is symmetric across methods, so the
  proposals' feasibility on the same points is the fair comparison (they also failed unit 3).
- Budget reading [S]: a stopped SFC has 8 units against 24; any later comparison is at unequal budget and must say
  so in the header of every table.

### 2.5 Fair reporting of a partial campaign, and bounded decision paths

Reporting rules that pretend nothing:

- Ordinal ledger: one row per method per ordinal 0 to 23, status in {pair-feasible, infeasible nominal,
  infeasible guard, failed, pending, not run, stopped}, J only for complete pairs, with the shared triple
  printed for ordinals 0 to 7. Never truncate to the executed prefix.
- Cross-method statements only at equal ordinals and identical triples (the shared initial units), which is the
  only place methods are directly comparable; after EI the incumbents are "each law's best found under its
  budget", and the budgets are printed.
- "Incumbent so far" is a running minimum J among pair-feasible units through ordinal k, always with k, never
  called best, never used as a baseline in a cross-method sentence before the freeze.
- The SFC unit-1 pair (J 65.6, the lowest of the 12) is neither a baseline nor hidden: it is one shared-triple
  observation, printed beside its absolute descriptors (load minimum 1.32 N against 1.96 N, progress 0.869
  against 0.886, post-release overshoot 1.88 mm against 3.49 mm, nominal path at 0.999 of the band).

Decision paths for main (conditional, no authorization implied):

- Path A: SFC gets at least one pair-feasible unit among 5, 6, 7. Nothing to decide; the frozen rules complete.
- Path B: SFC stops at 8. The freeze is unreachable under frozen code, so one versioned supersession is required.
  B1, reduced-budget baseline: declare that a stopped SFC's incumbent is the minimum-J pair-feasible unit among its
  8, which is the frozen proposer's own incumbent rule applied to a shorter history; the freeze precondition
  becomes "24, or 8 with a recorded stop"; SFC and SFC_RADIAL carry the label "8-unit initial-only baseline" in
  validation; no refund, no band change. B2, untuned seed: SFC and SFC_RADIAL run at the FT-v1 seed (unit 0),
  labelled "untuned seed"; the comparison becomes tuned proposals against an untuned baseline and is weaker.
  B3, halt: no freeze and no validation; the training is reported as a negative result on the band/stop interplay,
  and a v2 campaign with reconciled rules is a new pre-registered study.
- Recommendation: declare B1 now, before units 5 to 7 complete, and write both timings into it: prospective for the
  units that decide the gate, retrospective for units 0 to 4. B1 changes no observed value and consumes no unit.
  The cheapest error is to leave the gap undeclared until the stop occurs.

## 3. Q2: what is stable at fixed triples and what depends on the triple

### 3.1 The distinction kept explicit

Every comparison here is at the same (m, mu, g) for all three laws: unit 0 is the SFC seed, unit 1 the DSFC seed,
unit 2 the MSFC seed, unit 3 a shared Sobol point [S]. This is not the seed-versus-seed comparison of the earlier
studies and not the incumbent-versus-incumbent comparison the freeze will produce. DSFC and MSFC also share
a = 0.05, p = 0.5, n = 3 with each other, so at a shared triple they differ only in the active metric and its
solver tolerances [S].

### 3.2 Sign table over the four common units (B minus A) [D]

Stable in sign at 4 of 4, proposals (DSFC and MSFC) against SFC:

| Descriptor | DSFC − SFC by unit 0, 1, 2, 3 | Direction |
|---|---|---|
| Nominal path RMS, mm | −0.21, −0.25, −0.32, −0.03 | proposals more precise |
| Nominal progress | +0.022, +0.023, +0.027, +0.001 | proposals higher |
| Nominal attitude RMS, deg | −1.52, −1.57, −1.94, −0.14 | proposals lower |
| Nominal load MAE, N | −0.037, −0.026, −0.053, −0.010 | proposals lower |
| Nominal load peak, N | −0.16, −0.14, −0.42, −0.03 | proposals lower |
| Nominal load minimum, N | +0.36, +0.23, +0.28, +0.14 | proposals higher |
| Disturbed load minimum, N | +0.49, +0.64, +0.78, +0.53 | proposals higher |
| Time of disturbed load minimum, s | −1.5, −2.8, −1.8, −1.0 | proposals earlier (in the onset ramp) |
| Disturbed progress | +0.018, +0.017, +0.020, +0.004 | proposals higher |
| Disturbed saturation, s | −1.3, −1.1, −1.4, −2.0 | proposals less |
| Max post-release offset, mm | +0.40, +1.61, +0.79, +0.22 | proposals overshoot more |
| Recovery time, s (MSFC − SFC) | +0.29, +3.46, +0.85, +0.002 | MSFC slower; unit 1 is a 2 mm band crossing |

Stable in sign only at the three m = 4 units and reversed at unit 3:

| Descriptor | DSFC − SFC by unit 0, 1, 2, 3 |
|---|---|
| J, band-s | +8.4, +19.3, +13.2, −10.6 |
| Jload | +0.84, +0.90, +1.52, −1.73 |
| Jpath | +1.00, +5.09, +2.25, −5.14 |
| Jatt | +6.52, +13.27, +9.43, −3.71 |
| Disturbed attitude peak, deg | +3.04, +3.26, +2.25, −0.23 |
| Post-release attitude RMS, deg | +0.22, +0.81, +0.43, −0.24 |
| Recovery time, s (DSFC − SFC) | +0.33, +3.46, +0.85, −0.03 |
| QP intervention, s | +3.4, +5.5, +6.2, −0.06 |
| Hold mean load, N | −0.040, −0.048, −0.064, +0.032 |

Reading: the precision and load-margin orderings do not depend on the triple on this evidence; the J ordering and
the attitude-excursion ordering do, with the reversal at the low-damping triple where all arms are infeasible and
where the per-axis/radial distinction collapses (section 4.1). The m = 4 pattern is one pattern: SFC tilts less
under the hold, overshoots less after release and scores lower J, while dropping the load deeper and later and
saturating more. Nothing here is a benefit claim; the unresolved step sensitivity on this cell is unknown until
the post-freeze checks, and the SFC unit-1 recovery time of 0.0 s is a band artifact (section 4.3).

### 3.3 MSFC minus DSFC at a shared triple is the memory ablation at fixed coefficients

Identity-metric MSFC is the DSFC radial law with MSFC's own coefficients, matched to 4.6e-17 m/s [R]; in this
campaign DSFC and MSFC receive the same m, mu, g and the same fixed a, p, n, so DSFC-k is the identity arm at
triple k up to solver tolerances [S]. The training therefore already delivers the ablation at every shared initial
triple, in addition to the validation arm at the MSFC incumbent. On the four available [D]:

| Unit | ΔJ | ΔJload | ΔJpath | ΔJatt | λ_min minimum (time) | λ_min < 0.95 window | λ_min at release | Δ nominal attitude, deg | Δ saturation, s |
|---|---:|---:|---:|---:|---|---|---:|---:|---:|
| 0 | −0.548 | −0.042 | −0.197 | −0.309 | 0.815 (20.96 s) | 20.50 to 21.95 s (1.45 s) | 0.99961 | +0.017 | +0.55 |
| 1 | −0.014 | −0.025 | −0.006 | +0.017 | 0.832 (20.90 s) | 20.51 to 21.85 s (1.34 s) | 0.99991 | +0.005 | +0.13 |
| 2 | +0.153 | −0.003 | +0.012 | +0.143 | 0.850 (20.90 s) | 20.53 to 21.81 s (1.29 s) | 0.99969 | +0.014 | +0.20 |
| 3 | +0.791 | +0.015 | +0.275 | +0.501 | 0.808 (24.66 s) | 20.50 to 28.30 s (7.81 s) | 0.99955 | +0.001 | +0.04 |

Sign-unstable: J, Jpath, Jatt, post-release attitude, terminal offset, recovery. Sign-stable at 4 of 4, all tiny:
MSFC has higher nominal attitude (+0.001 to +0.017 deg), MAE (≤ +0.0015 N) and peak (≤ +0.013 N), lower nominal
load minimum (≤ 0.018 N) and more saturation (+0.04 to +0.55 s). The engagement window on this cell is 1.29 to
1.45 s at the m = 4 triples, consistent with round 7's 1.3 to 1.8 s at the seeds [R], and 7.8 s at the low-damping
triple, where the residual stays large through the hold; the largest and most adverse ΔJ is at that longest
engagement, with Jatt +0.50 concentrated in the release ramp and after (one triple, not a trend). Before onset the
disturbed member's 22-slot law state is bitwise the nominal's at all four units; at release the metric is within
0.0001 to 0.0004 of the nominal's [D]. The ablation series should be reported as "MSFC minus DSFC at shared
triples, training phase", with the caveat that no MSFC_IDENTITY trial exists in training and the equivalence is
the prior report's.

### 3.4 Not claimed

No benefit for any law; no resolution statement (the training cell's step sensitivity is unknown until the
post-freeze checks, and the nearest checked case moved 1.005 N [R]); no CI (deterministic runs, four points);
nothing about incumbents, which do not exist yet; nothing physical [P].

## 4. Q3: SFC-02 mechanics, and whether anything misreports

### 4.1 The nominal failure episode [D]

One-second bins, SFC-02 against DSFC-02, nominal (load range and MAE in N, normal-estimate error at bin end in deg,
motion updates per bin of 500 ticks):

| Bin, s | SFC load min to max | SFC MAE | DSFC load min to max | DSFC MAE | SFC estimate error | DSFC estimate error | SFC updates | DSFC updates | reference speed, mm/s |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|
| 3 to 4 | 4.50 to 5.60 | 0.316 | 4.78 to 5.28 | 0.148 | 1.08 | 1.14 | 500 | 500 | 4.06 |
| 5 to 6 | 3.96 to 6.05 | 0.628 | 4.51 to 5.48 | 0.288 | 1.22 | 0.65 | 449 | 500 | 3.53 |
| 6 to 7 | 3.94 to 6.14 | 0.722 | 4.39 to 5.60 | 0.370 | 1.85 | 0.25 | 323 | 500 | 3.23 |
| 7 to 8 | 3.96 to 6.14 | 0.668 | 4.34 to 5.70 | 0.416 | 3.02 | 0.52 | 323 | 500 | 2.93 |
| 8 to 9 | 3.99 to 6.09 | 0.684 | 4.31 to 5.72 | 0.452 | 4.28 | 0.97 | 284 | 476 | 2.65 |
| 9 to 10 | 4.12 to 5.96 | 0.554 | 4.35 to 5.70 | 0.392 | 5.44 | 1.67 | 216 | 300 | 2.42 |
| 10 to 11 | 4.29 to 5.86 | 0.455 | 4.54 to 5.56 | 0.306 | 6.22 | 2.34 | 138 | 129 | 2.23 |
| 11 to 12 | 4.45 to 5.74 | 0.380 | 4.76 to 5.34 | 0.164 | 6.38 | 2.35 | 29 | 0 | 2.11 |
| 12 to 20 | 4.59 to 5.60 | ≤ 0.31 | 4.83 to 5.18 | ≤ 0.09 | 6.40 to 6.41 | 2.36 to 2.38 | 0 | 0 | 2.0 |

Mechanism, in order: (i) the per-axis law's normal-direction activity in the 3 to 10 s segment is about twice the
radial law's (law normal-force RMS 0.70 to 0.75 N against 0.32 to 0.46 N; normal speed RMS 1.33 against 0.82 mm/s
over 5 to 10 s; tangential law force 0.16 to 0.23 against 0.08 to 0.13 N), so the load swings 2.2 N peak to peak
against 1.4 N [D]; (ii) the observer has no force correction (gain 0) and updates its normal from motion; as the
figure-eight slows from 4.1 to 2.0 mm/s the updates thin out and the estimate drifts, three times faster for SFC
whose motion carries the larger non-tangent component (1.2 to 6.4 deg from 5 to 12 s, against 0.65 to 2.4 deg)
[D][H for the causal link, S for the update rule]; (iii) from 12 s to 20.5 s no arm receives a motion update, so the
error is frozen through the onset (6.40 deg SFC, 2.36 deg DSFC; SFC-00 freezes at 5.0 deg, DSFC-00 at 1.7 deg), and
the whole-path attitude RMS is dominated by this plateau [D]; (iv) the frozen tilt costs path and progress in the
same segment (path RMS 8.0 mm against 5.6 mm in 10 to 15 s; progress 0.69 to 0.83 against 0.74 to 0.89 in 9 to 14 s)
[D]. The excess is law-specific and amplified by the triple: at unit 3 (mu = 43) SFC and DSFC fine bins agree to
0.15 N and 0.3 deg and both freeze near 5.3 to 5.6 deg, because at low damping the per-axis/radial distinction
collapses; at unit 4 (low g) SFC's attitude is fine (1.5 deg) and the failure is load [D].

A direct signature of the two law families: during the disturbed hold the median angle between law velocity and
law force is 24 to 43 deg for DSFC and MSFC (velocity follows force, radial) and 74 to 82 deg for SFC (per-axis),
and 89 deg for all three at unit 3 (undamped regime) [D].

### 4.2 The disturbed response and where SFC-02's lower J comes from [D]

| Quantity | SFC-02 | DSFC-02 | MSFC-02 |
|---|---:|---:|---:|
| J (Jload, Jpath, Jatt), band-s | 68.05 (45.21, 5.27, 17.57) | 81.25 (46.74, 7.52, 27.00) | 81.41 (46.73, 7.53, 27.15) |
| Jatt by phase: hold, release, post | 8.23, 5.10, 4.23 | 11.51, 8.81, 6.68 | 11.61, 8.84, 6.69 |
| Attitude peak, deg | 6.42 | 8.67 | 8.70 |
| Hold attitude RMS 25 to 30.5 s (estimate error), deg | 3.3 (3.6) | 6.1 (8.6) | 6.2 (8.7) |
| Post-release attitude RMS, deg (nominal) | 1.30 (0.72) | 1.73 (0.74) | 1.73 (0.74) |
| Load minimum, N (time) | 1.488 (22.40 s) | 2.264 (20.57 s) | 2.253 (20.57 s) |
| Load peak, N (= nominal, before 20 s) | 6.142 | 5.717 | 5.726 |
| Hold mean load, N (nominal 4.98 to 4.99) | 3.235 | 3.171 | 3.171 |
| Progress (nominal) | 0.863 (0.847) | 0.883 (0.874) | 0.883 (0.873) |
| Max post-release offset, mm (time) | 2.09 (37.26 s) | 2.88 (37.36 s) | 2.88 (37.36 s) |
| Recovery, s; terminal offset, mm | 2.15; 0.009 | 2.99; 0.021 | 2.99; 0.021 |
| Saturation, QP intervention, s | 2.10, 4.00 | 0.73, 10.18 | 0.94, 10.22 |
| Task scale mean 25 to 30.5 s | 0.77 | 0.58 | 0.57 |

The 13.2 band-s are Jatt −9.4 (hold −3.3, release −3.7, post −2.4), Jpath −2.2, Jload −1.5. Under the hold the radial
laws yield along the force, the motion-based observer rotates its normal by up to 8.7 deg, the attitude follows,
and the QP throttles the task for 4.7 s of the 5.5 s window; the per-axis law yields less along the force, tilts
less, and drops the load deeper and 1.8 s later. After release the radial laws overshoot to 2.9 mm at 37.4 s and
take 3.0 s to re-enter the 2 mm/0.5 N band; SFC re-enters at 2.15 s with a 2.09 mm overshoot. This is a genuine
performance tradeoff: J is computed as declared, the nominal is out of band as declared, and the two are reported
separately as the contract requires. The right sentence is "lower J, out-of-band nominal, deeper load minimum,
smaller attitude excursion", not "better".

### 4.3 Semantic audit of the current flags and objective [D][S]

Verified consistent: all 25 check sets equal their recomputation; `nominal_feasible_reported` on every disturbed row
equals its nominal member's flag; `pair_feasible` equals nominal-feasible AND guards on all 12; `disturbed_guards_ok`
is false exactly where the recomputed load minimum is below 1 N (SFC-03, 0.562 N, 0.61 s of contact loss);
`objective_eligible` is true for the 12 complete pairs and the objective is present exactly there; `full_cycle`
recomputes from the 31416-sample grid. No flag misreports a number.

Declared computations whose meaning is weaker than their name (report-level relabeling only; no metric change):

- Disturbed progress guard is inflated by the push. The oblique hold carries a forward tangential component:
  progress ratio 1.75 to 2.23 during the ramp, 1.30 to 1.36 during the hold, 0.26 to 0.42 during the release, net
  0.93 to 0.98 over the window against the nominal's 0.86 to 0.91 in the same window. If the window is replaced by
  the nominal's own progress, the SFC pairs at units 1, 2, 3 fall to 0.844, 0.841, 0.839 and SFC-00 to 0.853; the
  proposals at units 0 to 2 stay at 0.868 to 0.874; unit 3 falls to 0.846 for all. So SFC-01, a pair-feasible,
  selection-relevant unit, passes the disturbed progress guard only because of the push, and every SFC disturbed
  member has a post-release progress deficit that the push masks. The flag computes what it declares; it should
  be printed beside the in-window and outside-window ratios.
- Disturbed load-peak guard is inert on this cell. All 25 load peaks occur before 20 s; the maximum load after
  20 s is 5.01 to 5.96 N; the disturbed peak equals the nominal peak in all 12 pairs; the 8 N guard is never within
  2 N. On this cell "disturbed_force_peak" carries no disturbance information and the informative extreme is the
  minimum. The pulse cells in the reservation will behave differently (round 7 pulse peaks near 8 N [R]).
- Recovery time is a band crossing. SFC-01 reports 0.0 s because the pair is at 1.875 mm at release and never
  exceeds 2 mm afterwards; DSFC-01 is at 1.51 mm at release and overshoots to 3.49 mm at 37.5 s, hence 3.46 s. Eleven
  of 12 pairs reach their post-release maximum 1.8 to 2.1 s after release (2.1 to 4.2 mm); SFC-01's maximum is at
  release itself (1.88 mm); SFC's maximum is the smallest at all four triples. The graded
  quantities are the post-release Jpath and the maximum post-release offset; recovery_s should be printed with
  the distance at release and the overshoot.
- Labels: `objective_eligible: true` on a nominal member (no objective exists for one member) means "complete,
  uncensored member"; `pair_feasible_controls_selection: true` is a constant declaration on every row; the
  proposer's observation field named `nominal_feasible` carries pair feasibility after the ledger adapter [S].
  None changes a value; each can mislead a reader of a raw ledger dump and deserves a glossary line.
- J is dominated by the common hold-load floor: hold-phase Jload is 35.2 to 38.9 band-s of a 45.2 to 49.4 total in
  all 12 pairs, and the law-dependent Jload spread at a fixed triple is at most 1.7 band-s; the hold-phase path
  deviation (65 to 76 unweighted band-s) is excluded from J by the declared 35.5 s window [D][S]. This matches
  round 7's disclosure [R] and is why the report needs the phase decomposition beside J.

## 5. Q4: end-state if the original memory remains adverse

### 5.1 What the evidence already says about the mechanism's reach [D][S]

The law input is the force residual, so the metric leaves identity only during transients; on this cell it is
engaged for 1.3 to 1.5 s after the onset at the m = 4 triples and 7.8 s at the low-damping triple, it is within
0.0004 of the nominal's metric at every release, and the disturbed law state is bitwise the nominal's before
onset. The memory time constants (0.19 and 0.20 s) are two orders below the 10 s hold. Its measured effect at fixed
coefficients is at most 0.8 band-s of J, sign-unstable, with a sign-stable but tiny nominal-precision and
saturation penalty. Whatever the active metric does on the platform task, it does in the first one or two
seconds after a transient and in the divergence that seeds; the identity ablation captures exactly that.

### 5.2 The mechanism-versus-overfitting rule

A change is mechanism-justified only if all three hold: it is derived from a receipt-measurable state quantity
that does not depend on J (engagement window, eigenvalue floor reached, the ratio of the memory timescale to the
task transient); it predicts the sign of a named absolute descriptor's change before any run; and it is evaluated
on cells not used to choose it. A change is overfitting if it is selected because it lowers J on viewed cells, if
it adds a tuned degree of freedom to MSFC that the baseline does not receive, or if it alters the fixed memory
parameters after training or validation J has been seen. Viewed validation cells become development cells for
any design they influenced, as the reservation already states [R].

### 5.3 Minimal versioned change, in order of minimality

- Unresolved on every cell (the likely case given 0.01 to 0.8 band-s effects): no design change. The end-state is
  a reported null, "the active metric is inert to within resolution on the platform task", with the engagement
  windows printed as the reason. MSFC remains in the record as a documented mechanism that did not act.
- Resolved adverse on at least one validation cell, mechanism traceable to the engagement window (higher load
  minimum excursion, larger post-release attitude or path): the minimal change is subtractive, a versioned v2
  configuration fixing `minimum_metric_eigenvalue = 1`, so MSFC becomes the radial law at its own coefficients.
  It removes a mechanism rather than adding one, it is the one-bit decision the ablation was pre-registered to
  make, and it needs no new cell, controller or tuning. Overfitting exposure is limited to the ablation's own
  cells, which is why the resolution checks and the per-cell list, not a vote, decide it.
- Resolved beneficial somewhere and adverse elsewhere: report the per-cell list and change nothing; a mixed
  mechanism is not a default candidate.
- Only with a hypothesis written before viewing: a single memory-timescale change (for example a recovery time
  constant matched to the observed re-engagement window) as a new development study on the training cell, with
  the six validation cells kept unseen for it. This is the only positive change I would call
  mechanism-justified, and only under those conditions; anything chosen after viewing validation J is a refit.

### 5.4 Non-changes

No RNN promotion into the primary comparison (optional remains optional); no controller portfolio; no new
validation cells or scenarios; no reweighting of J or rebanding; no memory-parameter retune on viewed cells; no
intention-recognition, precision, damage or human-safety claim from any simulator result [P]. None of this is an
authorization to run anything now; the campaign and the validation writer proceed unchanged.

## 6. Negative and unresolved evidence preserved

- J ordering SFC against the proposals reverses at unit 3 (−10.6 band-s), so the m = 4 pattern is not a general
  ordering [D].
- MSFC minus DSFC changes sign across the four shared triples; the memory effect is not sign-stable on J [D].
- The disturbed progress guard passes every SFC pair only because of the tangential push; the peak guard never
  sees the disturbance on this cell; recovery time is a band crossing that reports 0.0 s for SFC-01 [D].
- SFC-03 lost contact for 0.61 s (load minimum 0.562 N) and the pair is correctly infeasible; DSFC-03 and MSFC-03
  reach 1.088 N, 9 percent above the guard [D].
- The training cell's step sensitivity remains unknown; the nearest checked SFC case moved 1.005 N [R].
- The frozen stop and freeze rules are unreconciled; an SFC stop has no path to validation without a versioned
  supersession [S].
- Formal units consumed: zero. Nothing here authorises motion [P].

## 7. Assumptions

- The identity of DSFC-k with MSFC_IDENTITY-k at a shared triple rests on the prior native-source verification
  (4.6e-17 m/s coefficient match) and the shared fixed a, p, n; solver tolerances differ (relative radius 1e-10
  against structure 1e-12), and no identity trial exists in training [R][S].
- The causal link from the per-axis law's normal-direction activity to the observer drift is inferred from the
  co-timing and the observer's update rule, not from an intervention [H].
- Rows are on the PATH clock; records aligned to rows are those whose reference phase is `path` (500 entry
  records precede them) [S].
- All numbers are properties of this deterministic simulator at 2 ms and 8 substeps; nothing transfers to the
  robot numerically [P].

## Reproduction

```bash
# from the experiment root; /tmp/yfp8 is a regenerable cache and is not retained
D=report/yield-fair-training-v1/discussion-round8; P=.venv-contact-six/bin/python
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
$P $D/scripts/extract_rows_v8.py                      # 25 listed artifacts -> /tmp/yfp8/<id>.npz (+ .meta.json), sha256 verified first
$P $D/scripts/analyze_round8.py            > $D/data/round8-analysis.json
$P $D/scripts/analyze_round8_mechanics.py  > $D/data/round8-mechanics.json
$P $D/scripts/round8_receipt.py            > $D/round8-input-receipt.json
```
