# Round 7 advisory: what the 144 training trials can mean, the reserved-cell audit, a memory ablation that needs no new waveform, and claim discipline for J

Role: advisory, non-blocking, no live authority; main owns goal, scientific acceptance, integration and hardware.
First of at most two iterations this round. HEAD 96a3c8bd at start and at receipt time; the worktree was clean at
start and main did not commit during the session. Written only under `report/yield-fair-tuning-v1/discussion-round7/`.
No runs, no native law stepped, no edits elsewhere, no Git writes, no subagents, no devices, no literature. The active
Grok repair worktree (`yield-fair-campaign-20260920`) was not opened; the campaign runner, selection evaluator and
campaign config it owns are absent at 96a3c8bd, and the committed pre-draft ledger (aeb9b61d) was not read. Round 6 (`../discussion/`) and main's adjudication of it were read and are preserved unchanged. At receipt time
main's integration of the repair had appeared in the worktree as uncommitted files (a modified ledger, the campaign
runner, selection evaluator, config, tests and two new report directories); none of them was read or touched, and
nothing here depends on them.

Inputs: 47 report/config files, 9 tool files and the native source (fingerprint 733c419b), all hashed in
`data/round7-input-hashes.json`; one input changed since round 6 (`advancement.json`, refreshed by main at f50c47c4).
Twenty-five raw receipts were used numerically through the round-5/6 tick caches plus three NO-v3 MSFC receipts
extracted this round; every cache hash was re-verified against the file on disk and against the owning study's
record (`data/round7-receipt-manifest.json`, all match). The warm-memory artifact and `check.py` match the hashes in
`result.json`. Derived numbers are in `data/round7-analysis.json` from `scripts/analyze_round7.py`.

Evidence classes: [S] structural (code, protocol, algebra); [D] re-derived here from retained receipts; [R] reported in
hashed reports, not re-derived; [H] hypothesis; [P] physical, not established by any simulator result.

## 0. Answers

1. **Q1, floor retained by all methods.** No pre-existing evidence contradiction makes the campaign pointless. The
   defensible reading, if every incumbent stays at about 5.3 mm path RMS and the post-tangential attitude floor, is:
   "under the frozen NO-v3 module, equal-budget J-driven tuning within the declared bounds did not move the nominal
   floor; the floor is attributed to the module by the observer-swap evidence (same laws, path RMS ×3.7 to ×4.5;
   same observer, law swap 4.2 percent [D]), not by the campaign, because J never rewards nominal precision; the
   campaign's own finding is the cross-law difference in disturbed response at that precision level, resolved or
   unresolved per the paired criterion." It is useful conditional evidence, with two disclosures: the hold-phase
   load floor of 65 band-seconds is common to all laws (spread 1.3 band-s [D]) and must be decomposed out of J in
   the report; and the one NO-v3 sustained SFC case ever step-checked failed the 0.5 N band (1.005 N [R]), so the
   training cell's resolution is unknown until the post-freeze checks. One report column is needed to make the
   reading honest: each incumbent's nominal descriptors against its own seed, because the bands are the SFC seed's
   and a DSFC or MSFC incumbent can drift from 1.2 to 2.8 deg nominal attitude while staying "feasible" [S].
2. **Q2, reserved six cells.** No fatal confound for within-cell cross-arm paired comparisons: every arm meets the
   same material, surface, prior, preparation, disturbance and three resolutions, with its own matched nominal,
   and the paired criterion is computable per cell. Across cells the design attributes nothing: of the 15 factor
   pairs, 11 are one-directionally aliased and the disturbance is fully aliased with the cell (section 3). That is
   lack of factor attribution, not invalid arm comparison. No essential factor is missing for what the design can
   serve; three rules are missing and should be added in a superseding protocol version before any run: training
   bands are flags, not gates, on validation cells; refinements are not run for a failed base pair; J is never
   pooled across cells (pulse J is 12 to 23 band-s, holds 80 to 120 at seeds [D]). The three (3,4)/m cells carry a
   declared risk that all arms fall below the 0.85 progress guard (nearest evidence: DSFC seed at (6,8)/m, progress
   0.745 [D]); that is a module outcome to report, not a reason to change values.
3. **Q3, memory causality.** The identity-metric arm at the MSFC incumbent, run on the same cells with the three
   resolutions, is the minimal valid causal ablation of the memory mechanism, and no new waveform is needed,
   because in closed loop the metric is engaged only for 1.3 to 1.8 s after a force transient and is within 1
   percent of identity everywhere else, including at onset (bitwise the nominal state) and at the release of every
   sustained hold (gap 0.0007 to 0.005 under NO-v3) [D]. What the identity arm cannot answer is the response to an
   externally pre-warmed memory, and no protocol scenario produces one at an intervention; that needs either a
   second transient within about 1 s of the first (new waveform) or a branch-from-record state intervention. The
   branch is feasible without controller redesign: restore controller and plant from a retained record, replace
   only slots 7 to 18 within the same binding, continue the runner loop; the identity switch mid-trajectory is not
   feasible without a declared bypass, because both the controller and the native law reject a snapshot whose
   identity or binding differs [S]. The precise gap is a runner entry point that starts from a record, plus a
   bitwise no-op branch check; the existing `replay_artifact` verifies determinism from the initial state only.
4. **Q4, J and feasibility.** Concrete retained counterexample: on the frozen oblique pulse, J ranks DSFC ahead of
   SFC (19.05 versus 23.33 band-s) while DSFC has the higher contact peak (8.23 versus 8.02 N) and the lower
   minimum load (1.228 versus 1.244 N), with progress 0.875 to 0.877 for both [D]; a peak excess of 0.2 N for
   0.1 s costs 0.04 band-s, below the 0.74 band-s the criterion resolves. Only the 8 N step guard separates these
   pairs from "feasible", and a 7.99 N peak is free. The relative attitude term gives each arm a credit for its own
   nominal error that differs between arms by 1.7 band-s at the seeds; on retained data it did not change any
   ordering, and that negative result is recorded. Reporting that avoids inflation without touching the metric:
   J with its three components beside the absolute descriptors (peak, minimum load, maximum attitude, post-release
   attitude RMS, terminal offset and load, recovery time) for every completed pair; "better" is allowed only when
   no absolute descriptor moves the other way by more than its own paired resolution, otherwise "mixed"; nominal
   descriptors of each incumbent against its own seed; no weight refit, no band move after viewing; any change is
   a versioned contract superseded before observation.

## 1. What changed since round 6 and what was taken as given

Main's round-6 adjudication is adopted in full: the single training cell with 24 literal pairs per method; separate
nominal and disturbed feasibility decisions with an explicit pair adapter; the three-term objective with 0.05 rad as
an explicit weighting convention, not a one-second correction guarantee; separated step checks after the freeze;
paired resolution with the unpaired sensitivity printed beside it; previously viewed conditions are development
transfer, and the six reserved cells are the prospective validation. The statement in round 6 that memory structure
falls to 5 percent within 0.6 s is withdrawn as main corrected it: with the coupled recurrence the structure retains
9.72 percent after 0.6 s from a held state [R], reproduced in the warm-memory artifact (0.0612 of 0.6297 [D]).

New evidence read this round [R]: native route repaired and bound to the complete runtime identity (42 + 27 tests);
full-cycle seam repaired with the bounded 4 ms unwrapped continuation (32 + 4 tests); fair proposer and ledger
verified with zero formal units; the six-cell reservation (`protocol.json`, sha 4454ce1a, 180 trials maximum,
zero executed); the warm-memory prescribed-history audit (12 histories, exact 22-slot replay, retained artifact
236fc41e); the campaign review with two counterexamples against the draft (null snapshots accepted by the
full-state helper; `pair_feasible=True` overriding `nominal_feasible=False` in a temporary ledger) and 2 of 16 draft
tests failing in `main-draft-tests.txt`. The repair is main's; nothing here depends on its outcome except that the
adjudication rules in section 5 assume the gates end up as an explicit conjunction, as main already requires.

## 2. Q1: interpretation before the 144 trials

### 2.1 Is there a pre-existing contradiction?

Three retained facts bound what the campaign can show, and none of them empties it.

- The nominal floor is a module property, already established without the campaign [D]: the same three seed laws
  under the legacy observer give 1.16 to 1.47 mm path RMS and under NO-v3 give 5.22 to 5.43 mm (×3.7 to ×4.5); the
  three laws under NO-v3 differ by 4.2 percent. Attitude splits the other way (2.56 / 1.16 / 1.31 deg NO-v3 versus
  6.5 deg legacy). J does not contain the nominal descriptors, so the optimizer has no gradient toward precision;
  a candidate that improved the floor would be feasible but invisible to selection. Therefore "the floor was
  retained" after the campaign is not evidence that the floor is law-inaccessible; the observer-swap fact is.
- The law-dependent part of J at the seeds is resolvable in principle [D]: J3 spreads across the three laws by
  40.1 band-s in the NO-v3 normal hold, 5.8 in the NO-v3 tangent hold, 9.7 in the frozen tangent hold, 11.2 in the
  pulse; the only pair with two settings varies by 0.078 band-s across settings (paired) and 0.83 to 0.91 (unpaired
  common drift). The common hold-phase load floor is 64.9 to 66.2 band-s for the three laws in the normal hold; EI
  is indifferent to it, readers are not, so the report must show the decomposition.
- The training cell's numerical behaviour is unknown [S], and the closest checked case is unfavourable: the SFC
  NO-v3 normal hold moved 1.005 N pointwise between 2 ms/0.25 ms and 1 ms/0.125 ms [R]. The oblique cell has a
  1.77 N outward component against 2.5 N there. If the incumbents fail the 0.5 N/2 mm bound on the training cell,
  the campaign yields tuned triples and an "unresolved" cross-law statement. That is a valid outcome, not a
  pointless one, provided it is written that way.

### 2.2 The defensible statement, and the one that is not

Defensible: "For each law, the best feasible (m, mu, g) on the oblique cell under identical modules, bands, schedule
and numerical setting; the cross-law differences in J and in each absolute descriptor at the three settings;
resolved or unresolved by the paired criterion; the nominal descriptors of every completed candidate, which show
whether m, mu, g moved the nominal floor within the explored region." The last clause is the campaign's only
by-product evidence on floor accessibility: 60 nominal trials over 20 distinct triples per method, including
band-failing ones, give a bounded negative or positive statement about the explored region, and only that region
(all seeds sit on the m = 4 bound [D]; an incumbent on a bound must be reported as such).

Not defensible: "no law improves precision" (never optimized for); "the laws are equivalent" (they differ in the
disturbed response at seeds); "memory helps or hurts" (needs the identity arm, which is in validation); any
physical statement [P].

### 2.3 One column that changes the reading

The bands are the SFC seed's values with step margins. DSFC and MSFC seeds sit well inside them (attitude 1.16 and
1.31 deg against 2.81; peak 5.89 and 5.73 N against 6.27). A DSFC or MSFC incumbent whose nominal attitude drifted to
2.8 deg is "feasible" and, because J is relative to its own nominal, may score well. If all three incumbents then sit
near the SFC band, the "retained floor" was partly created by selection. Report for each incumbent, and for each
completed candidate, the nominal descriptors as differences from the method's own seed. No band changes; a column.

## 3. Q2: audit of the six reserved cells

### 3.1 Design matrix and aliasing [S]

| Cell | Material | (κxx, κyy)/m | Prior | Angle | Preparation | Disturbance |
|---|---|---|---|---:|---|---|
| V1 | stiff | (1.2, 0.7) | approach | 0 | cold | sustained normal |
| V2 | compliant | (1.2, 0.7) | approach | 0 | cold | sustained tangent |
| V3 | stiff | (3, 4) | along | +7 | cold | sustained oblique |
| V4 | compliant | (3, 4) | across | −7 | cold | pulse normal |
| V5 | stiff | (1.2, 0.7) | across | +7 | warm | pulse tangent |
| V6 | compliant | (3, 4) | along | −7 | warm | pulse oblique |

Of the 15 factor pairs only material×surface, material×direction, material×preparation and surface×preparation
have at least two co-occurring levels in both directions, and those are 2:1 splits; the other 11 are one-directionally
aliased (approach only at 0 deg and only on (1.2, 0.7); along only on (3, 4); warm only with the tangent and oblique
pulses; stiff only with 0 or +7 deg, compliant only with 0 or −7 deg). Each disturbance appears once, so it is
aliased with everything. Consequence: no cell-to-cell difference can be attributed to a factor. This does not
affect a cross-arm comparison inside a cell, where all five arms share every factor level, the resolution settings,
and the evaluator, and each arm has its own matched nominal per the reservation. The design is a finite stress set
for arms, as the README says; it is not an estimator of effects.

### 3.2 What the arm pairs isolate

- MSFC versus MSFC_IDENTITY at the MSFC incumbent: the effect of the active metric with coefficients matched to
  4.6e-17 m/s [R]; asymmetric, because the triple was selected with memory on. If identity wins, memory hurt at
  that triple; if MSFC wins, memory helped or the triple co-adapted; both are honest sentences.
- SFC versus SFC_RADIAL at the SFC incumbent: per-axis versus Euclidean damping at the same triple. Caveat: the
  radial law is a Python implementation matched to the native law only in one dimension [S]; the resolution checks
  apply to it like any arm, and the report should say the 3D radial form has no native counterpart.
- DSFC incumbent versus MSFC_IDENTITY: the same law at two triples, one tuned for DSFC and one for MSFC. Free
  information on how far the tuning converged; not a pre-registered claim.

### 3.3 The (3, 4)/m cells and the warm cells

Nearest evidence for the (3, 4) surface is the DSFC seed nominal at (6, 8) under NO-v3: progress 0.745, path 7.03 mm,
attitude 6.0 deg, versus 0.875 / 5.22 mm / 1.16 deg at (0.8, 0.4) [D]. The normal-rotation rate on the figure-eight
is at most 0.012 rad/s at (3, 4) and 0.024 at (6, 8), under the 0.05 rad/s cap [S], so the (6, 8) degradation is not
the rate cap and the mechanism at (3, 4) is untested. Declared risk [H]: V3, V4 and V6 may fall below the 0.85
progress guard for all five arms, and their nominals will likely exceed the training-cell nominal bands for all
arms. Rule needed: on validation cells the training bands and the disturbed guards are reported as flags, and
the cross-arm comparison proceeds on descriptors with the resolution checks; an all-arm failure is a module
outcome for that cell.

Warm in V5 and V6 is the runner's 2 s stationary baseline before entry. It cannot produce a distinct memory state at
20 s: h and S sit 109 and 104 time constants downstream of the baseline [S], and in the retained receipts the law
snapshot before onset is bitwise identical between the disturbed and the nominal member [D]. What warm can change is
the observer and plant startup state, which is a legitimate startup factor and is what the reservation says it is;
the report must not call it a memory test. `compare_pair` requires the same preparation for both members [S].

### 3.4 Precise claim and failure logic with finite cells

- Unit of claim is (cell, arm A, arm B, descriptor). It exists only if both arms completed both members at all
  three resolutions on that cell; otherwise "not comparable at this cell" with descriptors printed.
- A cell where all five arms fail is a common-module result for that condition set; a cell where some arms fail is
  a named negative result for those arms. Failures are retained, never rerun.
- Aggregation across cells is a list, never a vote or a sum: "A ahead of B on cells {…} resolved, behind on {…},
  unresolved or not comparable on {…}". A single resolved reversal refutes "uniformly better".
- Refinement trials for a pair whose base member failed are not run and are recorded as "not run, base failed";
  this saves 4 trials per failed base pair and changes no value. Cost upper bound otherwise: 60 base trials at
  79 s, 60 controller refinements at 190 s, 60 plant refinements assumed at most 190 s: 7.6 worker-hours [R].
- Claim scope is fixed by the protocol constants that are not factors: 5 N setpoint, 20 s onset, 2.5 N sustained
  and 3 N pulse amplitudes. Nothing in the design speaks to other amplitudes, onsets or setpoints; say so rather
  than add cells.

No essential factor is missing for the purpose the design serves. The bounded recommendation is the three rules
above, written into a superseding protocol version before any trial.

## 4. Q3: a memory ablation with the existing task

### 4.1 What the memory state actually does in closed loop [D]

Law input is the force residual (filtered force plus injection minus the 5 N target along the estimated outward
normal, plus the integral term) [S], not the load. So in nominal contact the memory sees about 0.01 N and the metric
sits at identity; only transients move it. From the 22-slot snapshots in the retained receipts (MSFC g50, λ_min
0.0269):

| Receipt (sha first 12) | Observer, scenario | λ_min before onset | Minimum λ_min (time) | λ_min < 0.95 window | λ_min at release | Gap to nominal metric at release | Max gap after release |
|---|---|---:|---|---|---:|---:|---:|
| 8b490c06bc68 | NO-v3, nominal | 0.970 at 0.14 s (entry), ≥ 0.99 after 0.64 s | 0.970 (0.14 s) | none | 1.0000 | 0 | 0 |
| bbf2862bbc17 | NO-v3, sustained tangent | 0.970 | 0.791 (21.02 s) | 20.48 to 22.21 s (1.73 s) | 0.9993 | 0.0007 | 0.009 |
| 7079dae875ca | NO-v3, sustained normal | 0.970 | 0.970 (0.14 s, entry); 0.984 lowest in the hold | none | 0.9948 | 0.0052 | 0.006 |
| e764b57ab98a | frozen prior, sustained tangent | 0.970 | 0.784 (21.04 s) | 20.48 to 22.31 s (1.83 s) | 0.9993 | 0.0007 | within 1 percent at once |
| b5ff75ba1ba6 | frozen prior, oblique pulse | 0.970 | 0.869 (20.72 s) | 20.33 to 21.60 s (1.27 s) | 0.929 at 20.5 s | 0.071 | within 1 percent 2.0 s after |

Before onset the disturbed member's law snapshot equals the nominal member's bitwise in all four disturbed receipts.
During the NO-v3 tangent hold the residual stays at 0.2 to 0.3 N (observer error), which holds λ_min near 0.990 to
0.996, and the 5 s cosine release barely moves it (λ_min 0.990 at 33 s, below 0.99 for 0.34 s). The velocity state w is the warm quantity at
release: 0.039 m/s against 0.0023 nominal (×17), and w is shared with DSFC. The steady-state formula
λ = λ_min + (1 − λ_min)·exp(−κ τ_r |h|²) matches the plateau to 6.6e-7 [S][D].

Consequences. (i) A memory-state ablation at onset is a null intervention. (ii) At the release of a sustained hold
the memory is within 0.1 to 0.5 percent of identity, so a "hold-warmed memory" ablation at release is also nearly
null; the warm quantity there is w, a mechanical state. (iii) The whole effect of the active metric on any
existing cell is generated inside 1.3 to 1.8 s after the onset transient or the pulse, plus the trajectory
divergence it seeds. (iv) The identity arm on the same cell therefore captures the mechanism's entire causal
contribution under this protocol; there is no other memory state for the task to produce. This is consistent with
round 5: memory-on minus identity +0.147 N peak, J3 +0.74 band-s adverse on the pulse, 0.07 band-s on the frozen
tangent hold [D].

### 4.2 Warm startup, and what the warm-memory artifact shows [D]

In the prescribed-history artifact, after 2 s of zero input the held state keeps h = 7.1e-5 and S = 6.3e-5 (from 2.22
and 0.63) while w keeps 0.0285 m/s of 0.201; the effective decay time of h is 0.193 s throughout, S 0.39 then 0.22 then
0.20 s, and w 0.38, 0.63, 1.77 s (nonlinear damping, n = 3). The command difference warm-x versus cold at the 2 s gap,
0.0024473 m/s, equals g·Δw(0) = 0.0024489 m/s to 0.07 percent, at tick 0. So that residual difference is the velocity
state, entirely, as main's README says. A warm startup 21 s before onset leaves no trace in h or S [S]; whether it
leaves a trace in the observer is a startup question, answered by V5 and V6 as startup cells.

Warm memory states at an intervention do exist, but not in the protocol: they require the intervention to land
within about 1 s of a prior transient (λ_min recovers to 0.95 within 1.3 to 1.8 s). That is not "impossible"; it is
"absent from the six scenarios". Stating it as an explicit non-claim is enough for the campaign.

### 4.3 Forced resets and state surgery versus physical history [S]

- Arm-level identity ablation (in the design): supports "activating the metric pathway changes the closed-loop
  descriptor by Δ at this triple on this cell", resolved or not by the paired criterion. Mechanism claim; the
  strongest claim the protocol supports without new tooling.
- Branch-from-record with state surgery (not in the design): from the MSFC disturbed trial's record at a chosen
  tick, restore controller and plant in fresh instances and continue the runner loop; in one continuation replace
  slots 7 to 18 with a declared counterfactual (the matched nominal's h and S at the same tick, or zeros, which is
  the identity metric at that instant), keep w and everything else. Difference = causal effect of the memory state
  at that tick with identical plant, observer and w. Validity gate: the unmodified branch must reproduce the
  retained records bitwise, which the existing replay already shows is expected of this runner. Feasibility: the
  controller accepts a modified `law22.values` list with the same identity and the native restore accepts the same
  binding id [S]; a mid-trajectory switch to λ_min = 1 is rejected by both the controller identity check and the
  native binding check and would need a declared bypass, so it is not the minimal path. Missing piece: a runner
  entry point that starts from a record (restore both snapshots, continue `phases[k+1:]`, prepend the retained
  rows so J covers the whole PATH). This is tooling on the existing loop, not a controller change. Given 4.1, the
  only informative branch instants are inside the transient windows (for example 20.6 to 21.0 s) or a pre-warmed
  onset; branching at a sustained release tests a near-null state and would be reported as such.
- Physical history (different prior excitations within one trial): supports "history H1 versus H2 changes the
  response" but cannot separate memory from w and plant state, exactly as the artifact shows; within the protocol
  the only histories are cold/warm startup (inert for memory) and the scenario's own transient.

Answer to the direct question: the post-freeze identity arm plus the resolution checks meets the causal goal for the
task as defined, without a new waveform. Full-state replay as it exists adds determinism evidence, not a branch.
The remaining gap is narrow and named: a branch-from-record entry point with declared slot surgery, needed only if
main wants a state-level (not mechanism-level) statement or a pre-warmed-onset statement; and any such run is
explanatory unless pre-registered now.

## 5. Q4: where J and feasibility can hide damage, and how to report

### 5.1 Retained counterexample, force [D]

Frozen oblique pulse, 2 ms, four arms, all with progress 0.875 to 0.877:

| Arm | J3 band-s | Contact peak N | Minimum load N |
|---|---:|---:|---:|
| SFC | 23.33 | 8.02 | 1.244 |
| DSFC | 19.05 | 8.23 | 1.228 |
| MSFC | 12.88 | 7.83 | 1.343 |
| MSFC-identity | 12.14 | 7.68 | 1.372 |

J prefers DSFC to SFC by 4.3 band-s while DSFC has the higher peak and the lower minimum. A peak excess of ΔP for τ
seconds costs ΔP·τ/0.5 N band-s: 0.2 N for 0.1 s is 0.04, 2.9 N for 0.05 s is 0.29, against the 0.74 band-s the paired
criterion resolved on this pair. Feasibility is a step at 8 N (both SFC and DSFC would be infeasible here; a 7.99 N
peak is free) and at 1 N. Neither J nor the guards see the peak as a graded cost.

### 5.2 Attitude, and a negative result

No disturbed guard exists for attitude. In the NO-v3 tangent hold all three laws reach 11.4 to 12.2 deg during the
hold and keep 4.56 to 4.65 deg RMS after release against 0.73 to 0.75 nominal, with progress 0.88 to 0.90: feasible
[D]. J counts this inside J_att relative to the arm's nominal. The hypothesis that the relative form flips an ordering
was tested and did not hold on retained data: relative J_att 52.5 / 56.3 / 55.4 band-s and absolute 62.4 / 64.5 /
63.7 order the arms identically (SFC, MSFC, DSFC); the arm-dependent credit is 9.9 / 8.2 / 8.2 band-s, a 1.7 band-s
spread that exceeds the resolved J difference on the pulse pair but changed nothing here. It remains a mechanism
that can decide a close comparison, not a demonstrated one.

Arithmetic worth having in the report: a permanent post-release offset of 0.5 mm costs 6.8 band-s and counts as
recovered by the 2 mm metric; a 5 mm excursion for 2 s costs 5.0 band-s and recovers. J and the recovery time can
disagree on which is worse; both are reported, neither is the other.

### 5.3 Reporting and adjudication that keep the metric and protocol unchanged

- For every completed pair, training and validation: J3 and its three components; the absolute descriptors
  (contact peak, minimum load, maximum attitude, post-release attitude RMS, terminal offset and terminal load
  difference, recovery time with its censoring flag, saturation and QP seconds); the flags (guards, bands).
- Cross-arm wording: "better" only if J is resolved and no absolute descriptor moves against it by more than its
  own paired resolution on that cell; otherwise "lower J, mixed descriptors", with the descriptors named.
- Nominal descriptors of each incumbent against the method's own seed (section 2.3).
- Guards and bands stay as declared; they are gates in training and flags in validation; a change is a versioned
  contract superseded before any observation, never a refit after viewing, and weight sensitivity is reported as a
  limitation exactly as main adjudicated.
- The two draft counterexamples (null snapshots accepted; pair flag overriding a nominal rejection) are the kind of
  defect these rules cannot compensate for; the rules assume main's conjunction repair.

## 6. Negative and unresolved evidence preserved

- Attitude ordering under relative versus absolute J_att did not flip at the seeds (5.2).
- Memory engagement is confined to 1.3 to 1.8 s after transients; at seeds memory-on is inert or adverse (round 5,
  reproduced here); the identity arm carries this into validation.
- The warm-memory artifact's 2 s residual is the velocity state to 0.07 percent; no memory benefit claim.
- (3, 4)/m cells: nearest evidence at (6, 8)/m is progress 0.745 for the DSFC seed; all-arm guard failure is a live
  possibility on half the validation cells.
- SFC NO-v3 sustained normal hold: 1.005 N step sensitivity, 0.843 N minimum, unresolved [R]; the training cell's
  resolution is unknown.
- Draft campaign: 2 of 16 tests failing at the reviewed draft hashes, two acceptance counterexamples retained [R];
  repair in progress, not judged here.
- Formal units consumed: zero. Nothing here authorises motion [P].

## 7. Assumptions

- The reservation's per-arm matched nominal at each cell and resolution is taken from the README's "own
  nominal/disturbed pair" wording; if nominals were shared across arms the comparisons in section 3 would change.
- Memory engagement under NO-v3 is shown for the g50 seed on three cells; a tuned MSFC triple with larger g or mu
  could shift the residual, but the law input remains the residual, so the identity-at-nominal structure holds [S].
- The 2 ms/16-substep trial cost is assumed no larger than the 1 ms cost (not retained).
- The radial ablation's equivalence to native SFC is verified in one dimension only [S].
- All numbers are properties of this deterministic simulator; nothing transfers to the robot numerically [P].

## Reproduction

```bash
# from the experiment root; caches under /tmp are regenerable and not retained
D=report/yield-fair-tuning-v1/discussion-round7; P=.venv-contact-six/bin/python
# /tmp/yfp5 and /tmp/yfp6: see the Reproduction blocks of round 5 and round 6
mkdir -p /tmp/yfp7; X=report/yield-frozen-pulse-v1/discussion/scripts/extract_ticks_v5.py
$P $X runs/yield-observer-transfer-v1/MSFC-nominal.json.gz                    /tmp/yfp7/no3-nom-MSFC.npz
$P $X runs/yield-observer-transfer-v1/MSFC-sustained_release_tangent.json.gz  /tmp/yfp7/no3-tan-MSFC.npz
$P $X runs/yield-observer-transfer-v1/MSFC-sustained_release_normal.json.gz   /tmp/yfp7/no3-nrm-MSFC.npz
$P $D/scripts/analyze_round7.py > $D/data/round7-analysis.json
$P $D/scripts/round7_receipt.py > $D/round7-input-receipt.json
```
