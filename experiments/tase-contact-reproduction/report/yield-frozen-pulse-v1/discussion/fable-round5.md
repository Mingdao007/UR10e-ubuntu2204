# Round 5 advisory: stop iterating the deformation metric; the remaining differences are coefficient and threshold effects

Role: advisory, non-blocking, no live authority; main owns implementation, integration and acceptance.
Authoritative base b7008df4; HEAD at receipt time b84004ab (main's two concurrent commits touch the provider
seam, runtime model identity, tests and platform reports only). All 61 tracked report/tool/config inputs read
here are byte-identical between the two (`data/round5-input-hashes.json`). Development data only; no closed-loop
runs, no controller/test/config edits, no commits, no subagents. This directory holds read-only postprocessing
(`scripts/`) and derived numbers (`data/`); the per-tick cache is `/tmp/yfp5` (regenerated from retained
receipts by `scripts/extract_ticks_v5.py`, not retained). All 39 raw receipts used or cited were re-hashed
against their retained `results.json`/`protocol.json`/`validation.json` and match (`data/round5-receipt-manifest.json`).
The native law source at the path the build provenance records was re-hashed and matches the fingerprint bound
into every receipt (733c419b...), so the equations below are read from the code that produced the data.

Evidence classes: [S] structural (algebra, protocol definitions, controller/native code); [D] data re-derived
here from retained receipts; [R] numbers reported in earlier hashed reports, not re-derived; [H] hypothesis,
falsifiable but not established; [P] physical hardware statement, not established by any simulator result.

## 0. Answers

1. **Yes: the evidence justifies stopping deformation-metric iteration for this candidate**, and moving to a
   bounded fair-tuning/common-module comparison. Not because memory is shown useless in general (it is not),
   but because for this candidate the metric is (a) demonstrably engaged in every intervention scenario
   (minimum eigenvalue 0.87 pulse, 0.78 tangent hold, 0.99 normal hold), (b) mechanically inert or slightly
   harmful in twelve matched identity-metric pairs across three scenarios, two materials and two observers,
   and (c) its one measurable effect, the pulse rebound, has the sign its own equation predicts (section 3).
   There is nothing left to learn from another metric parameter set on these scenarios.
2. **No single minimal mechanistic change is worth testing on the metric.** The only change its mechanism
   suggests is reversing the sign of the structure update so it stiffens rather than softens along the loaded
   direction; that is a new law, not a tuning, and outside this study's remit. The smallest next research
   decision (section 8) is a fair comparison in which DSFC's search range covers MSFC's damping coefficients,
   with one pre-registered prediction: tuned MSFC minus tuned DSFC is within the numerical step sensitivity of
   the same cell. If that holds, MSFC's memory has no measurable role at any gain on this task.
3. **Gains/damping versus memory** [S][D]: the identity-metric MSFC is, by the native code, exactly the DSFC
   radial law with MSFC's coefficients (m=4, g=0.0858, p=0.5, a=0.05, n=3, mu=1228 versus DSFC's g=0.0642,
   mu=311). Every cross-method advantage attributed to MSFC in FP-v1 and FT-v1 is present in the identity
   variant, in some cases larger: pulse peak 7.68 versus 8.23 N (DSFC), recovery 2.30 versus 4.15 s,
   post-pulse envelope time constant 1.18 versus 1.78 s, nominal load MAE 0.040 versus 0.050 N. The memory
   contribution on top is +0.15 N peak and +0.05 s (2 ms) or +0.32 s (1 ms) recovery, i.e. adverse.
4. **Frozen-normal ablation versus usable controller** [D][R]: the frozen arm holds a fixed prior with
   1.36 deg RMS / 1.78 deg maximum error on the mild surface and 7.2 to 7.9 deg RMS with 0.53 to 0.72
   progress on the strong surface [R], at 5.0 mm path RMS and 0.87 progress against 1.0 to 1.5 mm and 1.02
   under the legacy observer [R]. It removed observer state so the laws could be compared; it is not a
   controller for the unknown-surface task, and none of its recovery numbers transfer to that task
   (NO-v3 gives 8.4 to 8.8 s for all three laws [R]).
5. **Threshold recovery versus robust load change** [D]: every pulse recovery is bound by the 0.5 N force
   band, never the 2 mm path band (the TCP is 0.4 to 1.5 mm from nominal at the last violation). The recovery
   time is therefore the moment a slowly decaying 1.0 to 1.6 Hz load oscillation last crosses 0.5 N. Under
   a 0.4 N band SFC goes from 4.53 to 10.80 s and the SFC/DSFC ordering inverts; under 0.3 N SFC is 15.2 s.
   The MSFC-family versus SFC/DSFC separation survives every band from 0.3 to 1.0 N and is carried by the
   envelope decay rate and the integrated load error, which are the robust quantities. The memory
   contribution to recovery (0.05 s at 2 ms, 0.32 s at 1 ms) is inside the threshold sensitivity; the memory
   contribution to peak (+0.147 N at 2 ms, +0.150 N at 1 ms) is not.
6. **Identifiable force during intervention** [S][D]: at the pulse peak the controller's sensed normal load is
   6.13 N while the true contact load is 3.80 N; the true load falls to 1.24 to 1.37 N in all four laws
   because each regulates the wrist sum. The rebound to 7.7 to 8.2 N is the consequence. Both extremes are
   common-module limits; the laws differ only in how they damp the rebound. The tangential filtered force
   reaches 1.72 N against 0.50 N of friction at the true load, so a declared friction-bound residual would be
   observable, but only with a declared mu-hat (the estimator currently assumes 0.35 while the plant uses
   0.15) and a dead zone that includes the prior-cone leak. It should be recorded, not gated (section 7).

## 1. Receipts and method

| Group | Receipts (SHA-256 first 12) | Use |
|---|---|---|
| FP-v1 pulse, 2 ms | SFC 5944e27df2e6, DSFC b8cfc3afe337, MSFC b5ff75ba1ba6, MSFC-identity 1f507fae4d44 | band binding, envelope, memory timeline, identifiability |
| Matched frozen nominals | SFC 4802cd3c4a5d, DSFC 234fe77c3966 (P0-v1), MSFC b953f4041fa4, MSFC-identity 6d415aa1c394 | pairing, nominal windows, observer error |
| FP-v1 refinement, 1 ms | MSFC c597127722ab / 6db73f5f915f, MSFC-identity 5769d72f25e0 / b429de054c55 | step sensitivity of the same descriptors |
| FT-v1 / FM-v1 tangent hold | SFC c7c5d28b7805, DSFC a85285e1cc35 (NO-v3 frozen cell), MSFC e764b57ab98a, MSFC-identity a883afdf6b16 | band binding, memory pair |
| GM-v1 legacy normal hold, g50, 8 substeps | on 333f7852a6f1 / 6b1a330dc527, identity a633cc5ab044 / ebb2816e9058 | memory engagement under normal hold |
| Cited only [R] | OT-v1 (9), DC-v1 (4), TR-v1 legacy comparators, GM-v1 g100 cells | tables in sections 2 and 5 |

Per-tick quantities: `rows` give evaluator truth (true load, true normal, external force); `records[*].result`
gives the law input u, the internal state w, the output g·w, the filtered force, the estimate and saturation
flags; `records[*].controller_snapshot.law22` slots 4 to 6, 7 to 9, 10 to 18 and 19 to 21 hold w, the force
history h, the structure S and the metric eigenvalues (checked: g·w from the snapshot equals the recorded law
output to machine precision). `scripts/analyze_round5.py` reproduces every number below into
`data/round5-analysis.json`; `compare_pair`'s recovery definition was reproduced exactly (4.526, 4.154, 2.352,
2.300, 2.629, 2.313, 1.004, 0.942, 1.008, 1.008 s) before any band was varied.

## 2. What the twelve matched memory pairs say

The ablation is `minimum_metric_eigenvalue = 1`. In `metric_from_structure` this gives every eigenvalue
1 + 0·exp(s) = 1 exactly, so A = I regardless of S; `smysfc_damping` then reduces to a·r^(p-1)·w +
mu·r^(n-1)·w, `solve_smysfc_mechanical` sees directional_metric = 1 and its radial solve is the `ysfc`
residual with MSFC's coefficients [S]. The history h and structure S keep evolving and are recorded but do not
touch the output. So "identity metric" is not a memory reset; it is a DSFC-form law with MSFC's parameters.

On minus identity, all deterministic single runs:

| Study | Observer | Material | Scenario | Δ peak N | Δ recovery s | Other | Class |
|---|---|---|---|---:|---:|---|---|
| GM-v1 g100 | legacy | stiff | normal hold | +0.007 | −0.13 (16.34 vs 16.47) | step sensitivity 3.60 vs 3.55 N | [R] |
| GM-v1 g50 | legacy | stiff | normal hold | +0.003 | +0.004 | | [R] |
| TR-v1 g50 | legacy | stiff | tangent hold | +0.005 | +0.004 | Δ yield +0.15 mm | [R] |
| TR-v1 g50 | legacy | stiff | pulse | +0.081 | +0.04 | | [R] |
| TR-v1 g50 | legacy | compliant | normal hold | +0.007 | 0.000 | | [R] |
| TR-v1 g50 | legacy | compliant | tangent hold | +0.007 | −0.006 | Δ yield +0.19 mm | [R] |
| TR-v1 g50 | legacy | compliant | pulse | +0.050 | −1.01 | no 1 ms check | [R] |
| TR-v1 / GM-v1 | legacy | both | nominal (3 cells) | +0.003 to +0.007 | n/a | | [R] |
| FM-v1 g50 | frozen | stiff | tangent hold | +0.012 | 0.000 | Δ MAE +0.0013 N, Δ yield +0.03 mm | [D] |
| FP-v1 g50 | frozen | stiff | pulse, 2 ms | +0.147 | +0.052 | Δ event MAE +0.053 N | [D] |
| FP-v1 g50 | frozen | stiff | pulse, 1 ms | +0.150 | +0.316 | | [D] |

Engagement is not the issue [D]. Metric minimum eigenvalue: pulse 0.869 (20.72 s), below 0.95 from 20.33 to
21.60 s, below 0.99 for 4.7 percent of the cycle; tangent hold 0.784 during the hold, mean 0.971; legacy g50
normal hold 0.988. In the tangent hold the on-minus-identity traces differ by at most 0.26 mm TCP and 0.073 N
true load during the hold and by 0.004 mm / 0.002 N after release, so the same recovery to the millisecond is
not a coincidence of thresholds: the trajectories have re-merged before the release ends.

The single favourable number, −1.01 s on the compliant pulse [R], is a legacy-observer threshold recovery
with no matched refinement and a +0.05 N peak on the same cell; by section 4 a recovery difference of that
size on a force-bound pulse is inside the band sensitivity. It is not evidence of a memory benefit, and it is
not evidence against one either; it is unresolved and cheap to resolve inside the fair comparison (section 8).

## 3. The pulse rebound is the metric doing what its equation says, with the wrong sign for a pulse

[S] S accumulates −kappa·h·hᵀ, so along the direction of recent force history the structure eigenvalue goes
negative and the metric eigenvalue λ = λ_min + (1 − λ_min)·exp(s) drops below one. Along that direction the
superlinear damping becomes mu·λ^((n+1)/2)·|w|^(n−1)·w; at λ = 0.87 and n = 3 that is 24 percent less
damping exactly along the axis the pulse loaded (GM-v1's `mechanism-hypothesis.md` states the same incremental
form). For a sustained push this is the intended yielding; for a half-second pull followed by a rebound it
means the rebound is under-damped.

[D] Pulse, on versus identity, PATH clock (pulse 20.0 to 20.5 s):

| Window s | eig min (on) | law output diff max mm/s | true load diff max N | true load max on / identity N |
|---|---:|---:|---:|---|
| 20.0 to 20.1 | 0.9999 | 0.00001 | 0.000005 | 5.03 / 5.03 |
| 20.2 to 20.3 | 0.962 | 0.027 | 0.0001 | 4.51 / 4.51 |
| 20.4 to 20.5 | 0.926 | 0.22 | 0.043 | 2.10 / 2.14 |
| 20.5 to 20.75 | 0.869 | 0.69 | 0.065 | 5.46 / 5.53 |
| 20.75 to 21.0 | 0.871 | 0.76 | 0.37 | 7.83 / 7.68 (peaks at 20.924 / 20.910 s) |
| 21.0 to 21.5 | 0.895 | 1.35 | 0.66 | 6.84 / 6.49 |
| 21.5 to 22.0 | 0.936 | 1.46 | 0.62 | 6.73 / 6.63 |
| 22 to 23 | 0.976 | 0.94 | 0.56 | 6.07 / 5.99 |
| 25 to 30 | 0.9994 | 0.16 | 0.08 | 5.17 / 5.16 |

The two variants are indistinguishable while the pull is applied (the law input is dominated by the pull and
the metric has not yet formed), separate as the metric bottoms out at the pulse end, and differ most during
the rebound, where the active metric's load runs 0.4 to 0.7 N higher for about 1.5 s. Timing, sign and
direction all match the mechanism; the 1 ms refinement reproduces the peak difference (+0.150 N) and widens the
recovery difference (+0.316 s), so the effect is not a discretisation artifact. This is the strongest
statement the data support: the deformation metric, as parameterised, softens the loaded axis after a pulse
and costs peak force. It does not show that a differently signed or differently timed memory would not help.

## 4. Threshold recovery versus robust load descriptors in the pulse

[D] All four 2 ms cells and both 1 ms cells are force-band bound: at the last band violation the TCP
distance to nominal is 0.40 (SFC), 0.53 (DSFC), 1.49 (MSFC), 1.45 mm (identity), the load difference is
0.500 to 0.507 N, and the recovery is unchanged under path bands of 1.5, 2 or 3 mm. The matched nominals carry
no oscillation that a phase shift could expose (load std 0.005 to 0.018 N in 15 to 20 s), so the post-pulse
|Δload| is a pulse-excited oscillation in the disturbed trial: dominant 1.0 to 1.6 Hz, amplitude 0.35 N (SFC),
0.44 (DSFC), 0.17 (MSFC), 0.15 N (identity) in 24 to 26 s, still 0.31 / 0.21 / 0.08 / 0.07 N in 30 to 35 s.

| Variant | Recovery at force band 0.3 / 0.4 / 0.5 / 0.6 / 0.8 / 1.0 N, s | Envelope stays below 1 / 0.5 / 0.25 N at s | Envelope τ (1 to 3 s fit) s | ∫abs(load−5) 20 to 23 / 23 to 35 N·s | Peak / min true load N |
|---|---|---|---:|---|---|
| SFC | 15.16 / 10.80 / 4.53 / 3.75 / 2.98 / 2.64 | 3.09 / 5.03 / 17.63 | 1.81 | 3.99 / 3.61 | 8.02 / 1.24 |
| DSFC | 8.41 / 4.88 / 4.15 / 3.48 / 2.80 / 2.46 | 2.96 / 4.65 / 11.01 | 1.78 | 4.07 / 2.63 | 8.23 / 1.23 |
| MSFC | 3.22 / 2.90 / 2.35 / 2.33 / 1.77 / 1.74 | 2.24 / 2.85 / 4.02 | 1.21 | 3.16 / 1.14 | 7.83 / 1.34 |
| MSFC-identity | 3.15 / 2.59 / 2.30 / 2.25 / 1.72 / 1.66 | 1.94 / 2.80 / 3.96 | 1.18 | 3.00 / 1.05 | 7.68 / 1.37 |
| MSFC, 1 ms | recovery 2.63 | 2.00 / 2.88 / 4.33 | | 3.20 / 1.42 | 7.85 / 1.33 |
| MSFC-identity, 1 ms | recovery 2.31 | 1.94 / 2.56 / 4.01 | | 3.04 / 1.30 | 7.70 / 1.36 |

Reading: the reported 4.53 / 4.15 / 2.35 / 2.30 s are crossing times of a decaying envelope at one band.
Moving the band by 0.1 N changes SFC by a factor 2.4 and flips SFC/DSFC; moving it by 0.5 N compresses all
four into 1.7 to 3.0 s. What does not move is the envelope decay rate (τ 1.8 versus 1.2 s) and the
integrated post-event load error (2.6 to 3.6 versus 1.0 to 1.1 N·s), which separate the MSFC-coefficient
family from SFC/DSFC by a factor 1.5 to 3 at every band and at both steps. Peak and minimum load differ by
0.2 to 0.55 N and 0.1 N respectively across laws; the memory adds +0.15 N to the peak at both steps.
[H] The 1.0 to 1.6 Hz mode is a lightly damped mode of the closed force loop (law, 20 ms filter, PI servo,
8000 N/m contact, 120 N/m path spring); its damping ratio from τ and frequency is roughly 0.06 (SFC/DSFC) to
0.09 (MSFC family). The same mode appears in the SFC nominal at 1.4 Hz with 0.016 N amplitude and dominates
the first 15 s of every nominal (section 6). [P] Its frequency and damping are properties of the assumed
servo and contact model and will not transfer numerically to the robot.

## 5. Tangent hold under the frozen observer does not discriminate the laws

[D] All four recoveries (1.004 / 0.942 / 1.008 / 1.008 s) are path-band bound; the post-release load
difference never exceeds 0.01 N. The TCP is 4.9 to 5.1 mm from nominal at the end of the release ramp and
crosses 2 mm about one second later in every law; at a 1 mm band the four are 1.41 to 1.51 s, at 3 mm 0.58
to 0.64 s. Steady yield against the 2.5 N push is 18.6 to 18.9 mm mean during the hold (peak 21.9 to 23.4 mm),
as the round-4 f_ext/Kp prediction (21 mm) implied. The only law-dependent tangent-hold quantity is the
full-cycle load MAE (0.169 / 0.065 / 0.049 / 0.047 N), and section 6 shows this is the entry transient, not
the hold. Under a frozen observer, therefore, the sustained-tangent scenario is a test of Kp and the
saturation cap, not of the law; the OT-v1 8.4 to 8.8 s and the legacy 10.9 / 2.7 / 0.5 s [R] were observer
state (round 4, confirmed by FT-v1). Nothing here ranks the laws for the unknown-surface task.

## 6. Nominal force error under the frozen observer is an entry-transient decay rate

[D] Load error relative to 5 N by window, matched frozen nominals:

| Window s | SFC MAE / std N | DSFC | MSFC | MSFC-identity |
|---|---|---|---|---|
| 0 to 2 | 0.506 / 0.585 | 0.392 / 0.478 | 0.279 / 0.355 | 0.274 / 0.349 |
| 2 to 5 | 0.326 / 0.363 | 0.185 / 0.205 | 0.128 / 0.144 | 0.126 / 0.141 |
| 5 to 10 | 0.405 / 0.451 | 0.214 / 0.238 | 0.183 / 0.204 | 0.180 / 0.200 |
| 10 to 15 | 0.177 / 0.222 | 0.072 / 0.095 | 0.066 / 0.088 | 0.065 / 0.086 |
| 15 to 20 | 0.013 / 0.018 | 0.005 / 0.006 | 0.004 / 0.005 | 0.004 / 0.005 |
| 20 to 62.8 | 0.008 / 0.009 | 0.008 / 0.009 | 0.008 / 0.009 | 0.008 / 0.009 |

After 20 s the four laws regulate identically to 0.001 N. The FT-v1 nominal MAE ranking (0.084 / 0.050 /
0.041 / 0.040 N [R]) is the decay of the entry-excited oscillation over the first 15 s. This is a real
regulation difference, but it is the same damping property as section 4 and it is not "accuracy on the
task". The frozen-observer path RMS (5.03 to 5.05 mm) and progress (0.872 to 0.873) are law-independent to
0.02 mm and 0.001, as round 4 predicted for a fixed Kp.

## 7. Coefficients, not memory: what the three laws actually are at their operating points

[S] With A = I all three laws are m·ẇ = u − D(|w|)·ŵ with D = a·r^p + mu·r^n (a = 0 for SFC; per-axis
SFC coincides with the radial form on a one-axis restriction). Continuous-form operating points with the
receipts' parameters:

| |u| N | SFC: steady g·r mm/s, dD/dr N·s, m/(dD/dr) s | DSFC | MSFC g50 (identity) |
|---:|---|---|---|
| 0.1 | 3.30, 4.7, 0.84 | 4.20, 4.1, 0.98 | 3.59, 6.6, 0.61 |
| 0.5 | 5.65, 13.8, 0.29 | 7.44, 12.6, 0.32 | 6.30, 20.0, 0.20 |
| 1.0 | 7.12, 22.0, 0.18 | 9.42, 20.1, 0.20 | 7.97, 31.9, 0.13 |
| 3.0 | 10.26, 45.7, 0.088 | 13.64, 42.1, 0.095 | 11.54, 66.6, 0.060 |

The MSFC candidate is a stiffer-damped law at every input level (about 1.6 times DSFC's incremental
damping) with an output mobility between SFC and DSFC. That is the whole of its cross-method behaviour in
FT-v1/FP-v1: a faster, better-damped rebound and a smaller entry transient. [H] The rebound-mode damping
follows the incremental damping at the rebound input level; this is consistent with the ordering in
sections 4 and 6 but is not a closed-loop proof (the filter, servo and contact enter the characteristic
polynomial). Nothing about h, S or A is required to explain any number in this round. Whether DSFC at
mu ≈ 1200, g ≈ 0.086 reproduces MSFC-identity exactly is not a hypothesis: by the native code it is the
same computation, and the FM-v1 native test already confirmed identity-metric MSFC equals matched DSFC [R].

## 8. Identifiability during external intervention, with pulse numbers

[S] The simulator's oblique axis is (outward + tangent)/√2 (`_external_wrench`), so the 3 N raised-cosine
pulse is a 2.07 N outward pull plus a 2.17 N push along travel, 45.0 deg from the true normal and 46.4 deg
from the frozen estimate (1.77 deg estimate error at that tick). [D] At the pulse peak (20.25 s) the
controller's signed normal load is 6.13 N and the true contact load 3.80 N (maximum sensed-minus-true
2.38 N). Every law withdraws to bring the sensed 6 N back to 5 N, so the true load falls to 1.24 (SFC),
1.23 (DSFC), 1.34 (MSFC), 1.37 N (identity); when the pull ends the sensed force collapses to the true value
and every law re-presses to 7.7 to 8.2 N. The 20 ms filter is not the cause (the pulse is 0.5 s). These
extremes are a property of regulating the wrist sum and are common to all laws; the laws differ only in the
rebound damping already discussed. No frozen-observer, memory or coefficient result changes this.

What is observable: the tangential filtered force in the estimated frame peaks at 1.72 N, against a friction
bound of 0.50 N at the true load and 0.53 N RMS in the nominal (regularised friction plus the 1.4 deg prior
leak). A declared friction-bound residual |P f| − mu_hat·|n·f| would exceed zero by about 1 N here. Its
costs, unchanged from round 4: it needs a declared mu_hat (the estimator's `assumed_friction_mu` is 0.35
while the plant's mu is 0.15; at 0.35 the dead zone 0.35·5 + 5·sin(10 deg) = 2.6 N would not fire on this
pulse, at 0.15 it would, marginally), and it says nothing about the normal component of the pull, which is
the component that drives the load excursion. Recommendation: record this residual as a diagnostic column in
every future cell so the fair comparison can report how often, and how far, each cell is outside the
declared friction cone; do not gate on it, and do not treat the simulator's ideal mu as validation. [P] On
hardware the same residual absorbs force bias, gravity-compensation error and payload error before it sees
a human.

## 9. Smallest next research decision and the evidence it needs

Decision: **close the metric question for this candidate and open the bounded fair comparison** on the
common modules and the full task, using the protocol's existing budget (8 initial + 12 BO + 4 repeat units
per method, separate holdout, at least 5 repeats [R]), with these pre-registrations, none of which change a
runtime, a gate or a criterion:

1. DSFC's parameter range must include MSFC's coefficient region (mu up to at least 1250, g up to at least
   0.09 at m = 4) so the search can find the identity-metric operating point. MSFC's four memory parameters
   and λ_min are either frozen at the candidate values or searched; either way the prediction to test is
   |tuned MSFC − tuned DSFC| ≤ the same-cell step sensitivity on every primary descriptor. If the prediction
   fails in MSFC's favour on a full-task cell after refinement, memory is doing something coefficients cannot
   and the metric question reopens with a mechanism to look for; if it holds, MSFC is a parameter version of
   DSFC and should be reported as such.
2. Step sensitivity as a constraint, not a footnote: each candidate parameter set is run at 2 ms/8 substeps
   and 1 ms/8 substeps (or 2 ms/16) with matched nominals, and a pre-declared bound rejects sets whose
   pointwise force difference exceeds it. The observed values are 0.12 to 0.13 N for the pulse cells here,
   0.044 N for MSFC g50 normal hold and 3.6 N for MSFC g100 normal hold [R]; the bound is main's to declare
   before the runs, not this round's to set after seeing them.
3. Descriptors reported alongside the unchanged 2 mm / 0.5 N recovery: envelope stay-below times at 1 N and
   0.25 N, integrated |load − 5 N| over [start, start+3 s) and [start+3, start+15 s), nominal load MAE
   split at 20 s, true-load minimum and peak, saturation and QP seconds, and the friction-bound residual of
   section 8. All are already computable from the receipts (`scripts/analyze_round5.py`). Recovery at one
   band should not be the optimisation objective for a force-bound scenario.
4. Observer: one versioned adaptive observer fixed for the whole comparison, with its round-4 contamination
   results attached. The frozen prior is not eligible as a comparison arm; if it is run at all it is a
   diagnostic column.
5. Materials and scenarios: both materials and all four scenarios, because the only memory-favourable number
   (compliant pulse, −1.01 s [R]) and the only method-inverting number (compliant tangent, SFC fastest [R])
   are both on the material the frozen arm never ran.

Evidence needed before or during this that is currently missing: a compliant-material matched refinement of
the TR-v1 pulse pair (resolves the −1.01 s inside the same batch); the declared mu_hat and its provenance;
and the step-sensitivity bound. None of these needs a new controller.

Cost and tradeoff: the fair comparison is the protocol's formal budget and cannot be reused as holdout; if
prediction 1 holds it ends MSFC as a distinct proposal on this task, which is a research-direction outcome
main must be willing to report. Continuing metric iteration instead costs one to two cells per variant and,
on this evidence, cannot produce a result that survives the identity ablation.

## 10. Stopping rules

For metric iteration on this candidate, stop when both hold, and both hold now: (i) on every full-task
scenario with a matched identity pair, the on-minus-identity difference is within the same cell's step
sensitivity or has an adverse sign; (ii) the largest difference is explained by the metric's own equation
(section 3). Reopen only on the failure of prediction 1 above.

For the candidate itself, respecting the full task: a proposal advances past development only if, under one
versioned adaptive observer, both materials, all four scenarios, equal-budget tuning and the step-sensitivity
constraint, it improves at least one pre-registered intervention descriptor and does not worsen nominal
accuracy, progress, peak load or minimum load beyond the step sensitivity, with repeats. Frozen-observer
results do not count toward this; the observer's own contamination (round 4) remains a common-module
problem to be reported with whatever law is chosen. No simulator outcome authorises motion or a safety claim [P].

## 11. What this round does not establish

- Nothing here validates the Coulomb contact, PI servo or joint-velocity clip; the rebound mode and its
  damping live in that model [P].
- All cells are single deterministic runs; the step check exists only for the two MSFC pulse cells and the
  DC-v1 SFC normal cell. SFC and DSFC pulse cells have no 1 ms check, so their envelope numbers are
  unrefined.
- The identity-metric law is "DSFC with MSFC coefficients" by code, not by an independent numerical run of
  DSFC at those coefficients under this harness (the FM-v1 native unit test is [R]).
- No compliant-material frozen or identity-metric pulse exists; the −1.01 s [R] remains unresolved.
- "Memory does not help" is claimed only for this metric at these parameters on these scenarios; a memory
  that stiffened rather than softened the loaded axis, or acted on a different feature than the compressed
  force history, is untested and out of scope.
- No law is declared better; sections 4, 6 and 7 describe a coefficient family, not a winner, and the fair
  comparison has not been run.

## Reproduction

```bash
# from the experiment root; cache under /tmp/yfp5 (not retained)
D=report/yield-frozen-pulse-v1/discussion; P=.venv-contact-six/bin/python; mkdir -p /tmp/yfp5
x() { $P $D/scripts/extract_ticks_v5.py "$2" /tmp/yfp5/"$1".npz; }
x pulse-SFC runs/yield-frozen-pulse-v1/SFC/SFC-short_pulse_oblique.json.gz
x pulse-DSFC runs/yield-frozen-pulse-v1/DSFC/DSFC-short_pulse_oblique.json.gz
x pulse-MSFC runs/yield-frozen-pulse-v1/MSFC/MSFC-short_pulse_oblique.json.gz
x pulse-MSFCid runs/yield-frozen-pulse-v1/MSFC-identity/MSFC-short_pulse_oblique.json.gz
x nom-SFC runs/yield-frozen-transfer-v1/SFC-nominal.json.gz
x nom-DSFC runs/yield-observer-prior-v1/DSFC-stiff_low_mu-approach.json.gz
x nom-MSFC runs/yield-frozen-transfer-v1/MSFC-nominal.json.gz
x nom-MSFCid runs/yield-frozen-memory-v1/MSFC-nominal.json.gz
x fine-pulse-MSFC runs/yield-frozen-pulse-v1-check/MSFC/short_pulse_oblique.json.gz
x fine-pulse-MSFCid runs/yield-frozen-pulse-v1-check/MSFC-identity/short_pulse_oblique.json.gz
x fine-nom-MSFC runs/yield-frozen-pulse-v1-check/MSFC/nominal.json.gz
x fine-nom-MSFCid runs/yield-frozen-pulse-v1-check/MSFC-identity/nominal.json.gz
x tan-SFC runs/yield-frozen-transfer-v1/SFC-sustained_release_tangent.json.gz
x tan-DSFC runs/yield-normal-v3/frozen-tangent-hold.json.gz
x tan-MSFC runs/yield-frozen-transfer-v1/MSFC-sustained_release_tangent.json.gz
x tan-MSFCid runs/yield-frozen-memory-v1/MSFC-sustained_release_tangent.json.gz
x gm-normal-g50on runs/yield-gain-memory-v1/MSFC-GM-v1-g50-on-sustained_release_normal-plant8.json.gz
x gm-normal-g50id runs/yield-gain-memory-v1/MSFC-GM-v1-g50-identity_metric-sustained_release_normal-plant8.json.gz
x gm-nom-g50on runs/yield-gain-memory-v1/MSFC-GM-v1-g50-on-nominal-plant8.json.gz
x gm-nom-g50id runs/yield-gain-memory-v1/MSFC-GM-v1-g50-identity_metric-nominal-plant8.json.gz
$P $D/scripts/analyze_round5.py > $D/data/round5-analysis.json
```
