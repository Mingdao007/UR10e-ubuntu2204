# OT-v1: observer choice changes the controller comparison

Six new full-period SFC/MSFC trials complete the three-method NO-v3 matrix,
using the already recorded three DSFC trials. Nine legacy-observer comparators
were hash-checked and matched in mechanical/controller settings, physical and
controller initial states, and references; only estimator parameters differ.
All eighteen are development trials with fixed existing parameters. This is
not equal-budget tuning or a final ranking. No controller was promoted and no
physical experiment ran.

The shared observer is not performance-neutral. Under legacy, tangent-release
recovery was SFC 10.926 s, DSFC 2.660 s, MSFC g50 0.460 s. Under NO-v3 it becomes
8.848, 8.424, 8.630 s. The large prior separation largely disappears. At the
same time, all methods have worse path accuracy/progress but lower normal
estimation RMS. The force MAE change depends on method and intervention.

A specific negative interaction appears under normal hold: SFC / NO-v3 has
contact load below 1 N for 0.434 s and takes 20.384 s to recover. The matched legacy SFC trial
had no load below 1 N and recovered in 1.176 s. NO-v3 DSFC and MSFC have no load below 1 N and recover in 3.834 and 1.122 s. This does not establish an intrinsic
proposal advantage: gains are existing candidates, baseline/proposal nominal
errors differ, and the result depends on the common observer. The negative
case receives a separate full-state replay and 1 ms check.

NO-v3 is therefore not promoted. The common-module dependency must remain
explicit in later tuning and holdout interpretation. It would be misleading
to report legacy MSFC recovery gains as observer-independent, or to replace
legacy with NO-v3 solely because its normal estimate is closer to truth.
The frozen observer is likewise not a solution to unknown curved surfaces.

The companion OC-v1 analysis exactly decomposes observed law-input differences
into measured-force, target-normal, path-displacement and path-projection
terms. After tangent release, the NO-v3 target-normal term alone has 0.350 N
RMS, but opposing terms leave total input difference only 0.104 N RMS.
Cancellation explains why small aggregate input/velocity curves do not imply
small internal effects. This is an algebraic decomposition of coupled
observations, not a causal intervention on the estimator.

Reproduce with tools/study_yield_observer_transfer_v1.py,
report_yield_observer_transfer_v1.py and plot_yield_observer_transfer_v1.py.
The raw full-state receipts are local under runs/yield-observer-transfer-v1;
results.json records raw paths, hashes, all metrics and nine invariant checks.
The PNG/PDF/SVG comparison has been rendered and inspected. Negative outcomes
and progress costs are retained. The simulator still lacks identified real
hardware dynamics, orientation-dependent contact patches, friction variability
and physical intervention evidence. No material-damage or human-safety claim.

The low-load case replays all 31,916 coarse records without mismatch.
At 1 ms it still completes the scheduled period but below-1-N duration
increases from 0.434 to 0.985 s. Maximum common-time contact-force difference
is 1.005 N and position difference 1.002 mm; 1 ms also adds 324 saturation
ticks. The negative phenomenon persists, but its magnitude is not numerically
settled. These data are unsuitable for quantitative baseline/proposal ranking.
No 1 ms recovery time is claimed because a matched 1 ms nominal was not run.
See contact-loss-check.json for the full metrics and raw receipt hash.

Correction: the historical field `contact_loss_duration_s` counts load below
1 N, not geometric separation. Evaluator checks find minimum loads 0.843 N
(2 ms) and 0.643 N (1 ms), with no zero-load samples and no nonnegative
geometric gap in either trace. Earlier wording of physical contact loss was
incorrect. Raw metrics and original receipts are unchanged.
