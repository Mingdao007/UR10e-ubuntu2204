# DC-v1: low load is not geometric separation; control-period sensitivity dominates

Two new cells complete a 2x2 numerical experiment using the same SFC law,
NO-v3 observer, normal intervention, initial state and physical reference.
Controller dt is 1 or 2 ms; plant integration step is 0.125 or 0.25 ms.
The two earlier diagonal cells are reused after raw receipt hash verification.
All four complete the full PATH cycle without execution failure.

The existing `contact_loss_duration_s` field counts normal load below 1 N.
Calling its nonzero value physical contact loss was incorrect. At 2 ms/0.25 ms,
minimum force is 0.843 N; at 1 ms/0.125 ms it is 0.643 N. The two new off-diagonal
cells have minima 0.822 N and 0.666 N. Every trace has zero duration at zero
force and zero duration at nonnegative geometric gap. Thus this evidence
establishes a low-load excursion, not geometric separation. The historical
metric values and raw receipts are retained unchanged; OT-v1 narrative/plot
labels have been corrected.

Holding plant step fixed, halving controller dt increases the below-1-N
metric by 0.501 or 0.491 s, force MAE by 0.042 or 0.043 N, and peak by about
0.12 N. Holding controller dt fixed, halving plant step increases the low-load
metric only 0.060 or 0.050 s and force MAE 0.004 or 0.006 N. This local factorial
comparison directs diagnosis toward control-chain sampling/discretization and
state/gate interactions. It does not uniquely identify the native law,
observer, QP or sample-and-hold as the cause. Two levels do not prove a
convergence order or an asymptotic continuous-time limit.

Saturation exposure is zero at 2 ms and 0.249/0.324 s at 1 ms. Counts are
converted to seconds in results.md so differing sample numbers are not
mistaken for extra exposure. Actual progress rises slightly at 1 ms, while
normal-load regulation worsens. No default or gain is changed.

The planned execution period remains 2 ms; hardware state was not probed. Sensitivity at another
period does not erase a valid fixed-period digital-controller observation;
it prevents claiming that the observed ranking is step-independent or solely
a property of the continuous control laws. These unqualified simulation
parameters and one-cell sensitivity check are insufficient for final ranking
or physical acceptance.

Reproduce via tools/study_yield_discretization_v1.py and
report_yield_discretization_v1.py. results.json retains all metrics, raw paths,
hashes and fixed-factor finite differences; protocol.json freezes source and
parameters. Curve geometry is evaluated only after runs and never fed to a
controller. No new runtime code, gates, clock rules or hardware operations
were introduced in this round.

A separate check of the round-4 advisory's strong causal wording reloaded and
hash-verified the two original DSFC traces. The fitted post-release displacement
slope is 0.72670 mm/degree, close to the quasi-static 5 N / 120 N/m estimate
0.72722. But the fitted intercept is 1.05212 mm. At recovery the actual TCP
difference is 1.99977 mm while the leak-only prediction is 1.33397 mm. Thus the
slope supports the proposed coupling mechanism but cannot establish an exact
dynamic identity or that the entire recovery delay is caused exclusively by
the observer. See recovery-leak-check.json and its reproducible helper.
