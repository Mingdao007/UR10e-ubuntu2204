# Main interpretation and next mechanism question

The terminal training campaign contains 8 SFC, 20 DSFC and 20 MSFC discovery pairs, plus four literal repeats each for DSFC/MSFC. Feasible discovery counts are 2/8, 5/20 and 7/20. Repeats are not new candidate discoveries or independent uncertainty samples. SFC stopped under the registered initial-feasibility rule; this is not a complete equal-budget comparison.

## Matched-coefficient development evidence

Values below come from the final report members.csv and slots.csv, units 0 and 1; all six pairs are feasible. Peak is whole-episode actual normal load, not necessarily the disturbance response peak. Recovery is relative to each method's own nominal trajectory with the registered tolerance, not absolute task completion.

| Unit | Method | Peak N | Minimum N | Disturbed path RMS mm | Recovery s |
| --- | --- | --- | --- | --- | --- |
| 0 | SFC | 6.2229 | 1.4248 | 7.079 | 3.652 |
| 0 | DSFC | 6.0597 | 1.9175 | 6.428 | 3.978 |
| 0 | MSFC | 6.0730 | 1.8994 | 6.423 | 3.944 |
| 1 | SFC | 6.0234 | 1.3204 | 7.037 | 0.000 |
| 1 | DSFC | 5.8863 | 1.9652 | 6.487 | 3.460 |
| 1 | MSFC | 5.8926 | 1.9565 | 6.496 | 3.456 |

These samples show a tradeoff rather than uniform dominance: lower peak/load variation and path error can coexist with slower paired recovery. SFC unit 1 recovery of zero means the release state already satisfies the registered paired recovery tolerance; it does not prove zero absolute error or instantaneous physical recovery. Nominal errors differ, so these are not equal-normal-accuracy comparisons.

MSFC versus DSFC is mixed. At shared unit 4 MSFC peak is 6.6956 N versus 6.5394 N and recovery 7.598 s versus 7.202 s, while its disturbed path RMS is lower (6.467 versus 6.567 mm). Both pairs are nominally infeasible and remain negative development evidence. A small difference in aggregate J is insufficient to establish memory benefit or inertness.

## Next bounded mechanism study

Before adding controller terms or repeating the search, isolate active memory and geometry on the already observed development surface and disturbance. Use the same shared coefficients, observer, outer loop, constraints and paired nominal references. Compare actual MSFC against its identity-metric arm and original per-axis SFC against radial SFC; retain DSFC as the matched reference. Include resolution checks before interpreting small differences. Freeze this as a separately named development diagnostic, with a fixed small case list and no optimization budget. Do not call it reserved validation or a new fair tuned comparison.

The existing reserved validation cells remain unexecuted and must not be repurposed silently. No current selection threshold, equation, budget or freeze requirement is relaxed. This note authorizes no hardware action and makes no causal conclusion from the observational table. Implementation and execution of the bounded diagnostic remain outstanding.
