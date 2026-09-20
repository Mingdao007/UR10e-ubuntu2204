# Round 10 advisory: what the terminal training dataset supports, and how far the 60-trial diagnostic can isolate memory and geometry

Role: advisory, non-blocking, no live authority; main owns integration, scientific acceptance, Git and hardware.
First of at most two iterations this round. HEAD was 4e5b36bf at the session start and 8dc090fc at receipt time;
main's three intervening commits (d76da76e, 29632fae, 8dc090fc) add the mechanism diagnostic tool and its frozen
protocol and change none of the inputs used here (checked by `git diff --name-only`). Written only under
`report/yield-fair-training-v1/discussion-round10/`; scratch cache `/tmp/yfp10`. No run, no native law stepped,
no ledger or SQLite opened, no `progress.json` read, no `runs/yield-mechanism-development-v1/` content opened, no
edits elsewhere, no Git writes, no subagents, no devices, no network. Round 9 attempt 1 exited 143 without an
advisory; it is not treated as completed and no cause is inferred.

Inputs: the committed final report (`report/yield-training-report-final-v1/`: `report.md`, `report.json`,
`members.csv`, `slots.csv`, `initial_triples.csv`, `best_so_far.csv`, `artifact_digest_manifest.csv`,
`main-interpretation.md`), `launcher-result.json`, `report/yield-round8-main-review-v1/main-adjudication.md` and
README, the adjudicated round-8 analysis JSONs, the committed campaign/tuner/observer configs and frozen evaluator
modules (all hash-match the launch binding), the reserved validation protocol v1, and, read after the start HEAD,
the committed diagnostic protocol `report/yield-mechanism-development-v1/README.md` and `frozen-campaign.json`
(protocol only; no lane receipt or run output). Raw: 34 of the 112 manifest members, one at a time, each hashed
against `artifact_digest_manifest.csv` before parsing: the shared initial triples 0 to 4 for SFC/DSFC/MSFC and
the shared EI triple DSFC-08/MSFC-08. Derived numbers are in `data/round10-analysis.json` (raw subset) and
`data/round10-shared-triples.json` (final CSVs only); scripts under `scripts/`; the input receipt is
`round10-input-receipt.json`.

Evidence classes: [S] structural (code, protocol, algebra); [D] re-derived here from hash-verified receipts or
the final CSVs; [R] reported in hashed prior reports, not re-derived; [H] hypothesis; [P] physical, not established
by any simulator result.

## 0. Answers

1. **Q1.** The dataset supports one concrete, coefficient-conditional task-level tradeoff, and it is not the one the
   whole-episode table shows. At the three m = 4 seed triples (units 0, 1, 2) the per-axis SFC tilts less under the
   oblique hold (attitude RMS −1.0 to −1.1 deg, peak −2.4 to −3.3 deg, in absolute terms, not only paired), rotates
   the normal estimate less (3.5 to 3.8 deg against 6.3 to 6.6 deg), overshoots less after release (1.9 to 3.4 mm against
   2.9 to 3.8 mm) and returns closer to the reference (post-release path RMS −0.1 to −0.4 mm, attitude −0.2 to
   −0.8 deg); it pays with a deeper load dip (minimum 1.32 to 1.49 N against 1.92 to 2.26 N), a larger error peak
   (+0.49 to +0.78 N), normal-axis actuator saturation (0.35 to 1.07 s against 0), 1.7 to 2.4 mm more displacement
   during the hold, and worse nominal precision before onset (path +1.0 to +1.3 mm, attitude +2.3 to +2.9 deg,
   MAE +0.08 to +0.16 N) [D]. Both sides are absolute descriptors, so the SFC Jatt/Jpath advantage is not a
   paired-reference artifact; what is an artifact of aggregation is the whole-episode table: the load "peak" is the
   nominal entry transient in all 17 pairs (disturbed and nominal members are bitwise identical before 20 s), and
   the whole-episode path and attitude RMS are dominated by pre-onset nominal differences whose sign is opposite to
   the post-onset one [D]. The tradeoff reverses at the Sobol triples 3, 4, 6, 7 (m ≥ 5.3 with low g/m or low mu),
   where SFC is worse on load and J and at unit 3 loses load (0.56 N, 0.61 s) [D]; so it is a seed-family statement,
   not a law statement. MSFC against DSFC is unresolved at all 14 shared triples (8 initial plus 6 exact EI
   coincidences): ΔJ is negative at 7 and positive at 7; the only sign-stable part is a nominal precision penalty
   for the active metric at 14 of 14 (MAE, peak, attitude up, load minimum down), tiny except where the metric is
   engaged in nominal contact [D]. Unit 4 is the one near-feasible triple where the metric is engaged from the entry
   through 12 s of nominal contact and is still engaged at release (λ_min 0.957), and it is where MSFC's adverse
   post-release differences are largest [D].
2. **Q2.** The frozen 60-trial list is sufficient to isolate the active metric at the two triples on this
   disturbance (MSFC minus MSFC_IDENTITY, with MSFC_IDENTITY minus DSFC as the implementation floor and the three
   resolutions as the numerical floor) and to isolate radial-versus-per-axis geometry at SFC's coefficients (SFC
   minus SFC_RADIAL), provided the report is phase-resolved and the contrast rule in section 3.2 is pre-registered.
   Every arm is necessary: DSFC alone is not the identity floor (different solver bindings), SFC_RADIAL is the only
   geometry ablation, and the resolutions are the only noise proxy. It is not sufficient for: the low-damping regime
   (triple 3, the longest engagement, 7.8 s), other disturbance directions, any incumbent or equal-budget claim, or
   separating law geometry from observer coupling, because every arm enters the disturbance from its own nominal
   history (frozen estimate error 5.0 deg for SFC against 1.7 deg for DSFC at unit 0). Confounders to log, not to
   add trials for: SFC_RADIAL is a Python explicit-Euler law against native C++ arms (the 1D match is asserted in
   the docstring and must be a bound check); per-axis saturation and QP scaling; the six base-resolution nominal
   members at units 0/4 should reproduce the training nominals bitwise if the binding is the same, a free
   integrity check.
3. **Q3.** One hypothesis [H]: MSFC's adverse differences from DSFC at shared coefficients arise only where the
   metric is engaged outside the onset transient, in nominal contact and at release, and are within the numerical
   floor where the metric has returned to identity before release. It predicts a resolved adverse MSFC minus
   MSFC_IDENTITY difference at triple 4 on nominal MAE/peak and on post-release overshoot and recovery, an
   unresolved one at triple 0, and MSFC_IDENTITY within the floor of DSFC at both. Section 4 gives the falsifiers
   and a retain/stop rule that needs no positive result and relaxes no gate.
4. **Q4.** Before any publication-grade cross-law claim: an equal-budget training re-registered as a new protocol
   with the same rules for all methods (not 8 against 24, not tuned against seeds), nominal-conditioned reporting
   (every disturbed contrast beside the nominal descriptors of the same pair, and claims limited to coefficient
   sets whose nominal descriptors fall within a pre-registered equivalence margin), resolution checks for every
   quoted difference (none exist for any training pair today), untouched reserved cells for any design informed by
   units 0/4 or the training table, phase-resolved descriptor semantics, and no uncertainty statement from
   deterministic repeats. Every numerical difference in this report is unresolved until then.

## 1. Inputs, verification, what was not read

- All 34 raw digests match the final manifest; all 34 `summarize_trial` recomputations equal the artifact metrics
  and `members.csv` to 1e-12; all 17 objectives recomputed with the frozen `_objective_components` equal
  `slots.csv` to 0.0; all 17 recovery times equal `slots.csv`; in all 17 pairs the disturbed rows are bitwise the
  nominal rows before 20 s and the whole-episode load peak equals the nominal's [D].
- The 18 bound tool files and the campaign, tuner and observer configs in the working tree hash-match the launch
  binding; the 25 artifacts of the round-8 snapshot have the same digests in the final manifest, so the round-8
  analysis that main reproduced exactly remains valid for units 0 to 3 and is cited rather than re-derived [D].
- Contract as bound [S]: nominal bands path RMS ≤ 5.48 mm, progress ≥ 0.85, attitude RMS ≤ 2.81 deg, load MAE
  ≤ 0.127 N, peak ≤ 6.27 N, minimum ≥ 1 N; disturbed guards minimum ≥ 1 N, peak ≤ 8 N, progress ≥ 0.85;
  J = Jload (from 20 s, 0.5 N) + Jpath (from 35.5 s, 2 mm) + Jatt (from 20 s, 0.05 rad); recovery is the first
  post-release time from which the paired offset stays within 2 mm and 0.5 N; oblique hold 20 to 20.5 s ramp,
  20.5 to 30.5 s hold, 30.5 to 35.5 s release, amplitude 2.5 N.
- Not read: the other 78 members (their values come only from `members.csv`/`slots.csv`), the SQLite ledger,
  `progress.json`, the round-9 snapshot, any diagnostic run, lane receipt or writer worktree.

## 2. Q1: phase-level evidence, all negatives, and what is an artifact

### 2.1 What the whole-episode table conflates [D]

Because each disturbed member is bitwise its nominal before onset, every whole-episode descriptor is a mixture of a
nominal-only segment (0 to 20 s, 32 percent of the samples) and the disturbance response. Three consequences for
the table in `main-interpretation.md`:

- "Peak N" is the nominal entry or early-tracking transient in all 17 pairs (SFC at 6.8 to 7.3 s at units 0 to 2;
  DSFC/MSFC at 0.09 to 0.22 s at units 0 and 1 and at 8.42 s at unit 2; all three at 0.29 s at unit 3 and 0.87 s at
  unit 4; DSFC/MSFC-08 at 8.17 s). The maximum load after 20 s is 5.06 to 5.96 N in every disturbed member; the 8 N
  guard is never within 2 N. Lower "peak" for DSFC/MSFC is a nominal statement.
- "Disturbed path RMS" 7.08 against 6.43 mm at unit 0 decomposes into pre-onset 6.28 against 5.30 mm (nominal),
  hold 10.88 against 9.19 mm (SFC displaced more by the push), and post-release 5.71 against 5.83 mm (SFC closer to
  the reference). Unit 1: 6.44/5.36, 11.41/9.66, 5.38/5.76 mm. The post-release sign is opposite to the other two.
- Whole-episode disturbed attitude RMS (3.17 against 2.98 deg at unit 0) is dominated by the pre-onset plateau
  (3.79 against 1.45 deg); after onset SFC is lower in every phase (next table).

### 2.2 Units 0 and 1 by phase, absolute descriptors (disturbed member; nominal in brackets where it differs) [D]

| Phase | Descriptor | SFC-00 | DSFC-00 | MSFC-00 | SFC-01 | DSFC-01 | MSFC-01 |
|---|---|---:|---:|---:|---:|---:|---:|
| pre (0 to 20 s) | path RMS mm | 6.28 | 5.30 | 5.31 | 6.44 | 5.36 | 5.36 |
| pre | attitude RMS deg | 3.79 | 1.45 | 1.48 | 4.04 | 1.65 | 1.66 |
| pre | load MAE N | 0.34 | 0.23 | 0.23 | 0.29 | 0.21 | 0.22 |
| hold (20.5 to 30.5 s) | load minimum N (time s) | 1.42 (22.2) | 1.92 (20.7) | 1.90 (20.7) | 1.32 (23.5) | 1.97 (20.6) | 1.96 (20.6) |
| hold | force error peak N | 3.58 | 3.08 | 3.10 | 3.68 | 3.04 | 3.04 |
| hold | mean load N | 3.20 | 3.16 | 3.16 | 3.21 | 3.16 | 3.16 |
| hold | path RMS mm | 10.88 | 9.19 | 9.18 | 11.41 | 9.66 | 9.69 |
| hold | attitude RMS / peak deg | 3.67 / 5.91 | 4.66 / 8.95 | 4.62 / 8.91 | 3.70 / 5.63 | 4.77 / 8.89 | 4.79 / 8.89 |
| hold | normal-estimate error RMS deg | 3.82 | 6.28 | 6.37 | 3.73 | 6.43 | 6.41 |
| hold | normal / tangent saturation s | 0.35 / 1.14 | 0 / 0 | 0 / 0.55 | 0.91 / 1.82 | 0 / 1.16 | 0 / 1.29 |
| hold | QP intervention s, mean task scale | 4.69, 0.77 | 5.92, 0.70 | 6.36, 0.68 | 3.00, 0.85 | 6.16, 0.69 | 6.15, 0.69 |
| hold | median angle law velocity to law force, deg | 73.9 | 27.2 | 27.9 | 81.9 | 41.8 | 42.8 |
| release (30.5 to 35.5 s) | attitude RMS deg | 4.67 | 6.10 | 6.05 | 3.10 | 5.94 | 5.93 |
| release | load minimum N | 2.89 | 3.09 | 3.09 | 2.88 | 3.13 | 3.10 |
| post (35.5 s to T) | path RMS mm [nominal] | 5.71 [5.19] | 5.83 [5.19] | 5.82 [5.19] | 5.38 [5.18] | 5.76 [5.18] | 5.76 [5.18] |
| post | attitude RMS deg [nominal] | 1.80 [0.73] | 2.02 [0.75] | 2.00 [0.75] | 1.13 [0.73] | 1.94 [0.75] | 1.94 [0.75] |
| post | attitude peak deg | 4.56 | 5.09 | 5.02 | 2.27 | 4.91 | 4.91 |
| pair | offset at release / max post-release offset mm | 2.92 / 3.42 | 3.03 / 3.82 | 2.96 / 3.76 | 1.88 / 1.88 | 1.51 / 3.49 | 1.53 / 3.49 |
| pair | recovery s | 3.65 | 3.98 | 3.94 | 0.00 | 3.46 | 3.46 |
| pair | J (Jload, Jpath, Jatt) | 77.45 (45.96, 9.00, 22.49) | 85.82 (46.80, 10.00, 29.02) | 85.27 (46.76, 9.80, 28.71) | 65.65 (46.09, 3.77, 15.79) | 84.90 (46.99, 8.86, 29.05) | 84.89 (46.97, 8.85, 29.07) |

### 2.3 The paired-reference test [D]

The concern was that a law with a worse nominal could score a lower paired J because its disturbed member is
"not much worse than its own already-bad nominal". The test is to put the unpaired integrals on the same windows
and scales beside the paired ones (`A_att_disturbed` is ∫|θ_d|/0.05 rad in band-seconds; `A_att_nominal` the same
for the nominal):

| Pair | hold Jatt / A_att_d / A_att_n | release Jatt / A_att_d / A_att_n | post Jatt / A_att_d / A_att_n | post Jpath / A_path_d / A_path_n |
|---|---|---|---|---|
| SFC-00 | 8.06 / 11.53 / 9.05 | 7.62 / 8.11 / 0.49 | 6.80 / 13.42 / 6.68 | 9.00 / 76.46 / 70.38 |
| DSFC-00 | 11.14 / 12.93 / 2.52 | 9.79 / 10.45 / 0.66 | 8.08 / 14.76 / 6.80 | 10.00 / 77.61 / 70.37 |
| SFC-01 | 8.07 / 11.88 / 9.91 | 4.65 / 5.14 / 0.49 | 3.06 / 9.63 / 6.66 | 3.77 / 72.49 / 70.23 |
| DSFC-01 | 11.63 / 13.37 / 3.06 | 9.57 / 10.21 / 0.64 | 7.85 / 14.37 / 6.78 | 8.86 / 76.42 / 70.24 |

SFC's unpaired disturbed attitude cost is lower than DSFC's in every post-onset phase (hold −1.4 to −1.5,
release −2.3 to −5.1, post −1.3 to −4.7 band-s), and its unpaired post-release path cost is lower by 1.2 to
3.9 band-s over an identical nominal floor (70.2 to 70.4). So the lower SFC Jatt and Jpath are not artifacts of
pairing against a worse nominal; pairing enlarges the SFC attitude margin (the paired hold gap is 3.1 to 3.6
against 1.4 to 1.5 unpaired) but does not create it. The one descriptor that is a band artifact is SFC-01's
recovery of 0.000 s: the pair is at 1.875 mm at release and never exceeds 2 mm, whereas DSFC-01 is at 1.51 mm
at release and overshoots to 3.49 mm at 37.5 s. The graded quantities, maximum post-release offset and post-release
path RMS, carry the same ordering, so "SFC recovers faster" should be written as "SFC overshoots less after release
(1.9 against 3.5 mm) and its recovery time is a band crossing".

### 2.4 The concrete tradeoff, its mechanism signature, and its domain [D]

At units 0, 1 and 2 (the three seeds, m = 4) one pattern holds in absolute terms: under the hold the radial laws
yield along the force (median angle between law velocity and law force 24 to 43 deg against 74 to 82 deg for the
per-axis law), the motion-based observer rotates its normal by 6.3 to 6.6 deg RMS against 3.5 to 3.8 deg, and the
attitude, which tracks the estimate, follows; the QP throttles the task longer (5.9 to 6.5 s against 3.0 to 4.7 s).
The per-axis law instead keeps its normal-axis command large enough to saturate the actuator (0.35 to 1.07 s),
drops the load 0.49 to 0.78 N deeper and 1.5 to 2.8 s later, and is displaced 1.7 to 2.4 mm more during the hold.
After release the radial laws overshoot more (round 7 located the warm quantity at release in the shared velocity
state w [R]) and take longer to re-enter the band. The task-level content is therefore: attitude excursion and
post-release overshoot (per-axis better) against contact-loss margin, actuator headroom and nominal precision
(radial better). Neither side is a declared precision or safety tolerance; which side matters is a task
decision main owns.

Domain: the sign table over all 8 initial triples (`data/round10-shared-triples.json`), SFC minus DSFC:

| Unit | m, mu, g | ΔJ | Δ disturbed load min N | Δ disturbed attitude peak deg | Δ max post offset mm | Δ nominal MAE N | Δ nominal attitude deg | SFC pair |
|---|---|---:|---:|---:|---:|---:|---:|---|
| 0 | 4.0, 393, 0.052 | −8.4 | −0.49 | −3.04 | −0.40 | +0.037 | +1.52 | feasible |
| 1 | 4.0, 311, 0.064 | −19.3 | −0.65 | −3.26 | −1.61 | +0.026 | +1.57 | feasible |
| 2 | 4.0, 1228, 0.086 | −13.2 | −0.78 | −2.25 | −0.79 | +0.053 | +1.94 | nominal infeasible |
| 3 | 13.2, 43, 0.156 | +10.6 | −0.53 | +0.23 | −0.22 | +0.010 | +0.14 | both infeasible; SFC load 0.56 N for 0.61 s |
| 4 | 5.32, 729, 0.028 | +9.2 | −0.28 | +0.22 | −0.45 | +0.124 | +0.62 | nominal infeasible |
| 5 | 7.64, 249, 0.077 | −18.7 | −0.43 | −2.31 | −2.11 | +0.026 | −0.09 | nominal infeasible |
| 6 | 11.0, 984, 0.009 | +54.9 | −0.08 | +0.23 | +0.93 | +0.432 | +0.01 | nominal infeasible |
| 7 | 12.1, 186, 0.013 | +9.7 | −0.59 | +0.72 | −0.68 | +0.545 | +0.97 | both infeasible |

Stable at 8 of 8: SFC has the deeper disturbed load minimum and the worse nominal MAE, peak and path. The
attitude-excursion and J advantage holds at units 0, 1, 2, 5 (m ≤ 7.6, g/m ≥ 0.010) and reverses at 3, 4, 6, 7.
So the tradeoff is a property of the seed family on this cell, and the statement "per-axis geometry trades load
margin for attitude excursion" is licensed only there; at low g/m the per-axis law is worse on every axis and the
distinction between the families collapses (unit 3: angles 89 deg for all three, round 8 [R]).

### 2.5 MSFC against DSFC: fourteen shared triples, not eight [S][D]

The frozen proposer scores a fixed 512-point scrambled-Sobol acquisition pool (seed 20260920 + 1009) that is
shared by all methods, so when two methods' GP fits are close their EI maxima land on the same pool point. That is
what happened: DSFC-08 = MSFC-08, DSFC-09 = MSFC-09, DSFC-10 = MSFC-16, DSFC-12 = MSFC-14, DSFC-14 = MSFC-13,
DSFC-16 = MSFC-12 have bitwise-equal registered (m, mu, g). The report's sentence "EI rows are not
coefficient-matched pairs" is true of the design and false of the data; these six are post-hoc matched
comparisons and should be disclosed as such. With the 8 initial triples that gives 14 memory ablations at fixed
coefficients (DSFC and MSFC share a, p, n; identity-metric MSFC is the DSFC radial law up to solver bindings [R]):

| DSFC / MSFC unit | m, mu, g | ΔJ | ΔJpath | ΔJatt | Δ recovery s | Δ max post offset mm | Δ nominal MAE N | Δ nominal peak N | Δ nominal attitude deg | Δ disturbed load min N | states |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 0 / 0 | 4.0, 393, 0.052 | −0.55 | −0.20 | −0.31 | −0.03 | −0.06 | +0.0015 | +0.013 | +0.017 | −0.018 | both feasible |
| 1 / 1 | 4.0, 311, 0.064 | −0.01 | −0.01 | +0.02 | −0.00 | +0.00 | +0.0005 | +0.006 | +0.005 | −0.009 | both feasible |
| 2 / 2 | 4.0, 1228, 0.086 | +0.15 | +0.01 | +0.14 | +0.00 | +0.00 | +0.0007 | +0.009 | +0.014 | −0.011 | both feasible |
| 3 / 3 | 13.2, 43, 0.156 | +0.79 | +0.28 | +0.50 | +0.04 | +0.09 | +0.0000 | +0.000 | +0.001 | +0.000 | both infeasible |
| 4 / 4 | 5.32, 729, 0.028 | +0.91 | +0.98 | −0.02 | +0.40 | +0.69 | +0.0103 | +0.156 | +0.076 | −0.072 | both infeasible |
| 5 / 5 | 7.64, 249, 0.077 | −0.21 | +0.18 | −0.38 | −0.03 | +0.04 | +0.0003 | +0.005 | +0.006 | −0.097 | both infeasible |
| 6 / 6 | 11.0, 984, 0.009 | −2.85 | −3.82 | +0.82 | −0.67 | −0.06 | +0.0309 | +0.095 | +0.042 | −0.106 | both infeasible |
| 7 / 7 | 12.1, 186, 0.013 | +2.31 | −0.88 | −0.73 | n/a | −0.06 | +0.0864 | +0.068 | +0.123 | −0.202 | both guard-infeasible |
| 8 / 8 | 4.74, 2114, 0.120 | +0.07 | +0.02 | +0.06 | +0.00 | +0.01 | +0.0009 | +0.009 | +0.019 | −0.005 | both feasible (both incumbents' region) |
| 9 / 9 | 5.14, 1227, 0.167 | −0.64 | +0.74 | +0.52 | n/a | +0.43 | +0.0145 | +0.148 | +0.118 | −0.400 | infeasible / guard-infeasible |
| 10 / 16 | 6.62, 1100, 0.158 | −0.74 | −0.26 | −0.41 | −0.07 | −0.09 | +0.0010 | +0.004 | +0.007 | −0.000 | both infeasible |
| 12 / 14 | 6.03, 576, 0.199 | −193.4 | −116.0 | −104.7 | n/a | −16.5 | +0.0213 | +0.265 | +0.084 | +0.000 | both unstable (J 565 / 371) |
| 16 / 12 | 4.23, 830, 0.133 | +4.49 | +1.86 | +2.18 | n/a | +0.13 | +0.0214 | +0.131 | +0.110 | +0.255 | both unstable (J 471 / 476) |
| 14 / 13 | 5.06, 1634, 0.134 | +0.11 | +0.04 | +0.07 | +0.01 | +0.01 | +0.0017 | +0.003 | +0.029 | −0.001 | both infeasible |

ΔJ is negative at 7 and positive at 7; Jpath and Jatt are sign-unstable; recovery differs by at most 0.07 s
except at units 4 (+0.40) and 6 (−0.67). Sign-stable at 14 of 14 is the nominal precision penalty of the active
metric (MAE, peak and attitude up, load minimum down), at the four pair-feasible triples of order 0.001 N, 0.01 N
and 0.01 deg, far below any resolution check that exists. At the shared feasible EI triple the two incumbents'
neighbourhood shows no difference beyond 0.07 band-s, 0.002 s and 0.006 mm; this is unresolved, not equivalence.

Where the metric is engaged (law22 slots 19 to 21, λ_min) [D]:

| MSFC member | entry λ_min | pre-onset s below 0.99 (below 0.95) | hold minimum (s below 0.95) | λ_min at release | release ramp s below 0.95 | post-release s below 0.99 |
|---|---:|---:|---:|---:|---:|---:|
| 00 nominal / disturbed | 0.943 | 1.25 (0) | 1.0 / 0.815 (1.45) | 1.000 / 0.9996 | 0 | 0 |
| 01 nominal / disturbed | 0.952 | 0.74 (0) | 1.0 / 0.832 (1.34) | 1.000 / 0.9999 | 0 | 0 |
| 02 nominal | 0.965 | 0.64 (0) | 1.0 | 1.000 | 0 | 0 |
| 03 nominal / disturbed | 0.931 | 7.97 (0.35) | 1.0 / 0.808 (6.99) | 1.000 / 0.9995 | 0 | 0 |
| 04 nominal / disturbed | 0.862 | 11.90 (1.68) | 1.0 / 0.754 (2.71) | 1.000 / 0.957 | 0.92 | 1.45 |
| 08 nominal / disturbed | 0.968 | 0.80 (0) | 1.0 / 0.864 (1.21) | 1.000 / 0.9999 | 0 | 0 |

Unit 4 (g = 0.028, the lowest g/m among the near-feasible triples) is the only one where the metric is engaged
through the nominal (λ_min 0.86 at the end of the entry, mean 0.90 to 0.98 for the first 10 s) and is still
engaged at release in the disturbed run. Its nominal excess over DSFC (MAE +0.010 N, peak +0.156 N at 0.87 s,
attitude +0.15 deg pre-onset, load minimum −0.10 N) is spread over 0 to 10 s where λ is below 0.99, and its
disturbed differences are qualitative as well as numerical: the QP intervenes for 2.6 s in the hold (DSFC 0 s),
the pair is 6.63 mm from its nominal at release (DSFC 5.93 mm), post-release Jpath is +0.98 band-s and recovery
+0.40 s. Unit 3 has the longest hold engagement (7.0 s) but returns to identity before release and its
post-release difference is a third of unit 4's. Unit 0 has 1.45 s of hold engagement and a favourable ΔJ.
So engagement length in the hold does not predict the sign; engagement in the nominal and at release is the
receipt-measurable state that separates unit 4 from the rest, which is why section 4 builds on it.

### 2.6 Statements the dataset does not support

- Any unqualified "better disturbance rejection" for either family: the load side and the attitude/overshoot side
  point in opposite directions at the seeds, and both reverse at low g/m.
- "Lower load peak under disturbance" for DSFC/MSFC: the peak is nominal; the disturbance load descriptor is the
  minimum, and it favours the radial laws.
- Recovery time as a graded result: SFC-01's 0.000 s is a band crossing (section 2.3).
- Any memory benefit or inertness: 14 ablations with sign-unstable ΔJ and no resolution check.
- Any statement about the 78 members not read here beyond what `members.csv`/`slots.csv` say.

## 3. Q2: the frozen 60-trial diagnostic

### 3.1 As frozen (read read-only after the start HEAD) [S]

`frozen-campaign.json` binds exactly the prompt's design: stiff_low_mu (0.8, 0.4), approach prior, NO-v3, cold,
Kp = 120, Kz = 0, full period, triples 0 and 4 with the same (m, mu, g) on every arm, arms SFC, SFC_RADIAL, DSFC,
MSFC, MSFC_IDENTITY (actual method MSFC, frozen MSFC parameters, metric floor 1), nominal plus sustained-release
oblique, resolutions 2 ms/8, 1 ms/4, 2 ms/16, 60 slots, zero training or validation budget, data-selection
disclosure for unit 4, training execution identities re-bound. Nothing below asks to change it.

### 3.2 Exact comparisons and the contrast rule to pre-register [S]

Per triple k ∈ {0, 4}, per resolution r, per phase (pre, onset, hold, release, post) and per descriptor x:

- G1 geometry at SFC coefficients: SFC − SFC_RADIAL.
- G2 law form at radial geometry: SFC_RADIAL − DSFC (same m, mu, g; SFC form has no a, p terms; Python against
  native).
- M1 active memory at MSFC coefficients: MSFC − MSFC_IDENTITY (the primary memory contrast).
- M2 implementation floor: MSFC_IDENTITY − DSFC (expected near zero; the prior equivalence is 4.6e-17 m/s on a
  1D check [R], never shown at full period).
- R numerical floor per arm: the spread of x across the three resolutions within one arm and member.

Descriptors, all already in the receipts: load minimum and its time, maximum after onset, error peak, hold mean
load; path RMS; attitude RMS and peak; normal-estimate error RMS; normal-axis and tangent saturation seconds; QP
intervention seconds and mean task scale; median angle between law velocity and law force; for the MSFC arms the
λ_min series (entry minimum, pre-onset seconds below 0.99, hold minimum, value at release); paired: Jload, Jpath,
Jatt by phase, offset at release, maximum post-release offset and its time, recovery with its censoring flag.

Rule: a contrast C = arm A − arm B on x is "resolved" only if |C| at base exceeds
R_x = max(spread_A, spread_B, |M2| at base) and C has the same sign at all three resolutions; otherwise it is
"unresolved", reported with its numbers, never "equivalent" or "inert". This uses no threshold from the training
contract and changes none.

### 3.3 Sufficient for, and not sufficient for [S][D]

Sufficient, under the rule above, to say at triples 0 and 4 on this disturbance whether the active metric changes
the phase descriptors beyond the numerical and implementation floors (M1 against M2 and R), and whether radial
geometry at SFC's coefficients removes the per-axis signatures seen in training (angle 74 to 82 deg, normal
saturation, deeper minimum, smaller attitude excursion). Each arm is necessary: without MSFC_IDENTITY, M1 would be
measured against a different solver binding; without SFC_RADIAL there is no geometry ablation; without the two
refinements there is no floor at all, since no training pair has a resolution check. Triple 4 is necessary because
it is the only near-feasible triple with nominal and release engagement; triple 0 is the seed and the case where
the metric is idle in nominal, so it is the control.

Not sufficient for: (i) the low-damping regime, triple 3, where engagement lasts 7 s and the families collapse;
(ii) normal, tangent or pulse disturbances, which belong to the reserved cells and must not be consumed;
(iii) any incumbent, equal-budget or superiority statement; (iv) separating law geometry from observer coupling:
every arm enters onset from its own nominal history with a different frozen estimate error (unit 0: 5.0 deg SFC,
1.7 deg DSFC; no motion update from about 12 s to 20.5 s in any arm [R]), so G1 and M1 measure "law plus the
nominal state it produces", not the law at an identical state; a branch-from-record entry point does not exist
in the runner [R] and is not requested.

### 3.4 Confounders to log or check, none of which needs a new trial [S][D]

1. SFC_RADIAL is `RadialSfcLaw`, a Python explicit-Euler law with previous-state Euclidean damping, fingerprint
   `python-radial-sfc-ablation-matched-to-native-1d`; SFC, DSFC and MSFC are the native C++ laws. The 1D match to
   native `sfc_step` is asserted in the docstring. G1 therefore confounds geometry with implementation unless a 1D
   equivalence check on a recorded force trace is bound into the diagnostic's identity (the native smoke check
   exercised the interface, not equivalence [R]).
2. Per-axis saturation (SFC 0.35 to 1.07 s normal-axis at the seeds) and QP task scaling (MSFC-04 2.6 s against
   DSFC-04 0 s in the hold) are arm-dependent nonlinearities inside the controller; report them per phase so a
   resolved contrast can be attributed to the law or to the limits it hits.
3. The whole-episode peak and the disturbed progress guard are inert or push-inflated on this cell [R]; print the
   maximum after onset and the windowed progress beside the registered values.
4. The six base-resolution nominal members at units 0 and 4 for SFC, DSFC and MSFC are re-runs of training members
   under the same bound sources, libraries, observer and outer loop; if the runner is deterministic they should
   reproduce the training rows bitwise. Either outcome is informative: identity confirms the binding, a difference
   is a direct measurement of the run-to-run floor and must be added to R_x.
5. MSFC_IDENTITY at triple 4 tests the nominal excess directly: if the metric causes it, MSFC_IDENTITY-04's
   nominal MAE, peak and pre-onset attitude fall within M2 of DSFC-04's; this needs no disturbed member.
6. Triples 0 and 4 differ in all three coefficients; no coefficient effect can be read from them.
7. Deterministic single runs: identical repeats add nothing; R is the only uncertainty proxy and must be reported
   as such [S].
8. The six EI coincidences already give M1-type evidence (DSFC as identity proxy) at six more triples for zero
   cost, at base resolution only; cite them as context, not as diagnostic results.

## 4. Q3: one falsifiable hypothesis and a retain/stop rule

### 4.1 Hypothesis [H]

MSFC's adverse differences from DSFC at shared coefficients are caused by metric engagement outside the onset
transient, in nominal contact and at release, and are absent within the floor where the metric returns to identity
before release. The receipt-measurable predictor is λ_min: engaged through the nominal and at release at triple 4
(0.86 at entry, 0.957 at release), idle in nominal and identity at release at triple 0 (0.943 at entry only,
0.9996 at release).

Predictions for the diagnostic, with the rule of 3.2:

- P1 at triple 4: M1 is resolved and adverse for MSFC on nominal MAE, nominal peak, pre-onset attitude, maximum
  post-release offset, post-release Jpath and recovery; MSFC's λ_min at release is below 0.99 at all three
  resolutions while MSFC_IDENTITY's is 1 by construction.
- P2 at triple 0: M1 on the same descriptors is unresolved; any resolved M1 is confined to the hold and release
  phases (the training ablation there is ΔJ −0.55 with 1.45 s of engagement).
- P3 at both triples: M2 is within R for every descriptor.

Falsifiers: F1, MSFC_IDENTITY-04 reproduces MSFC-04's nominal excess or its post-release overshoot within R (the
metric is not the cause); F2, M1 at triple 4 is unresolved (engagement at this magnitude has no effect beyond the
floor; the hypothesis is not supported and the training difference was noise-level); F3, a resolved adverse M1 at
triple 0 on nominal or post-release descriptors (engagement is not the predictor); F4, M2 resolved at either
triple (the identity-equals-DSFC premise fails at full period and the memory ablation logic must be re-bound
before any interpretation).

### 4.2 Retain/stop rule for the active-memory mechanism (not for any controller promotion) [S]

Evaluate after all 60 slots, on the pre-registered descriptors of P1 to P3, with R from the same data:

- Retain (the active metric stays a live mechanism worth one further pre-registered development study, and
  nothing else): P1, P2 and P3 all hold.
- Stop (record the metric's effect at these coefficients as unresolved or falsified; drop it as a mechanism to
  pursue; do not promote MSFC_IDENTITY or DSFC as a new design on this basis; keep the reserved cells untouched):
  F2 or F3, or P1 with sign changes across resolutions.
- Re-bind before deciding: F1 or F4 (the premise or the identity arm is wrong; the diagnostic's result is a
  binding finding, not a mechanism finding).

The rule needs no positive result: "stop" is a complete, reportable outcome; "unresolved" is reported with its
numbers and never as equivalence; no training band, guard, objective, budget, freeze or validation precondition is
touched; a later removal of the active metric would still be a versioned selection informed by viewed development
data, as main's round-8 adjudication already states.

The geometry contrast G1 gets pre-registered expected signatures without a second hypothesis: at both triples
SFC_RADIAL should show a hold-phase law angle near the radial arms' 24 to 43 deg, zero normal-axis saturation, a
load minimum within R of DSFC's, and a larger attitude excursion than SFC. Whether it does is the data's answer.

## 5. Q4: evidence still needed before publication claims

1. Equal-budget fair comparison. The reserved validation's own parameter rule ("three incumbents from completed
   equal-budget training") cannot be met by this campaign: SFC stopped at 8 of 24 under the registered rule, so no
   freeze exists and none can be manufactured. A new equal-budget protocol with identical rules for all methods,
   registered before any run, that retains this campaign's negative outcome and discloses that its design used
   these development data, is the only route (main's adjudication, decision 3). Not 8 against 24; not the
   FT-v1 seeds against tuned proposals; not a cross-campaign pool.
2. Equal-normal-performance conditioning. At every shared triple the per-axis law's nominal precision is worse
   (section 2.4), so every disturbed contrast here is at unequal nominal accuracy. A publishable cross-law
   disturbance statement needs either coefficient sets per law whose nominal descriptors fall within a
   pre-registered equivalence margin, or the statement restricted to "at identical coefficients" with the nominal
   descriptors printed in the same row. The former is a new protocol; the latter is what the development data
   can carry now.
3. Resolution. No training pair has a resolution check; the two post-freeze checks of the round-6 contract were
   never reached. Every difference quoted in this report, including the 0.1 to 0.4 mm and 0.2 to 0.8 deg
   post-release SFC advantages and every MSFC minus DSFC number, is unresolved until a check at the compared pairs
   exists. The diagnostic supplies this for 5 arms at 2 triples only.
4. Independence. Units 0 and 4 and the whole training table are viewed development data; the diagnostic's
   results are development. The reserved cells V1 to V6 remain the only unviewed evidence and must not be opened
   for design, hypothesis selection or descriptor choice; any design informed by the diagnostic needs its own
   frozen protocol before those cells are run.
5. Descriptor semantics in every table: maximum load after onset beside the registered peak; windowed progress
   beside the guard; offset at release and maximum post-release offset beside recovery; pre-onset and post-onset
   attitude and path separately; Jload's law-independent hold floor (35 to 39 of 45 to 49 band-s) disclosed beside J.
6. Uncertainty. Literal repeats are bitwise (main verified DSFC-08/20 and MSFC-17/20); they carry no uncertainty.
   Numerical sensitivity is the minimal proxy; the physical five-repeat requirement stands [P].
7. Scope. No physical, safety or precision claim; the bands are development bands.

## 6. Not claimed

No benefit for any law; no resolution statement; no CI; nothing about incumbents beyond the shared EI triple; no
causal claim for the coupling mechanism in 2.4 (the diagnostic's G1 is the test); nothing about the diagnostic's
execution state; nothing physical [P].

## 7. Reproduction

From the experiment root with `.venv-contact-six/bin/python` (numpy 1.24.4, Python 3.10.12, `OMP_NUM_THREADS=1`):

```
python report/yield-fair-training-v1/discussion-round10/scripts/extract_rows_v10.py \
    SFC-00-nominal SFC-00-disturbed DSFC-00-nominal DSFC-00-disturbed MSFC-00-nominal MSFC-00-disturbed \
    SFC-01-nominal SFC-01-disturbed DSFC-01-nominal DSFC-01-disturbed MSFC-01-nominal MSFC-01-disturbed \
    SFC-02-nominal SFC-02-disturbed DSFC-02-nominal DSFC-02-disturbed MSFC-02-nominal MSFC-02-disturbed \
    SFC-03-nominal SFC-03-disturbed DSFC-03-nominal DSFC-03-disturbed MSFC-03-nominal MSFC-03-disturbed \
    SFC-04-nominal SFC-04-disturbed DSFC-04-nominal DSFC-04-disturbed MSFC-04-nominal MSFC-04-disturbed \
    DSFC-08-nominal DSFC-08-disturbed MSFC-08-nominal MSFC-08-disturbed        # ~5 s each, hash-verified, -> /tmp/yfp10
python report/yield-fair-training-v1/discussion-round10/scripts/analyze_round10.py > data/round10-analysis.json   # ~12 s
python report/yield-fair-training-v1/discussion-round10/scripts/shared_triples_round10.py > data/round10-shared-triples.json
python report/yield-fair-training-v1/discussion-round10/scripts/round10_receipt.py > round10-input-receipt.json
```

The extractor reads only manifest-listed paths under `runs/yield-fair-training-v1` and refuses a digest or size
mismatch; the analysis imports the frozen evaluator functions read-only and refuses a cache whose recorded
artifact digest differs from the manifest. Output digests are in the receipt.
