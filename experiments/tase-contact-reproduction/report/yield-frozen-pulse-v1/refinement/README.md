# FP-v1 matched refinement

Both controller period and plant step are halved together: 2 ms / 0.25 ms to 1 ms / 0.125 ms. Each pulse uses a same-period, same-parameter nominal. No gain changes, frozen observer in all cases.

| Period ms | Variant | Force MAE N | Peak N | Path RMS mm | Progress | Saturation s | Recovery s |
|---|---|---:|---:|---:|---:|---:|---:|
| 2 | MSFC | 0.107577 | 7.832292 | 4.974163 | 0.875505 | 0.996000 | 2.352000 |
| 2 | MSFC-identity | 0.102724 | 7.684897 | 4.976152 | 0.875365 | 0.930000 | 2.300000 |
| 1 | MSFC | 0.123818 | 7.845893 | 4.971528 | 0.875533 | 1.004000 | 2.629000 |
| 1 | MSFC-identity | 0.118197 | 7.695709 | 4.973692 | 0.875390 | 0.936000 | 2.313000 |

Active-minus-identity contact peak is +0.147394 N at 2 ms and +0.150184 N at 1 ms. The direction of the negative metric result persists. Full-period force MAE also remains higher with the active metric.

Absolute recovery is not numerically settled: active changes from 2.352 to 2.629 s, whereas identity changes from 2.300 to 2.313 s. The active-minus-identity recovery gap therefore changes from 0.052 to 0.316 s. Do not quote a converged magnitude or use this threshold metric alone as a smooth optimization objective.

Common-time maximum load differences are 0.126081 N (active) and 0.123408 N (identity), and maximum TCP differences are 0.032736 mm and 0.030754 mm across the pulse trials. These are sensitivity measurements, not automatic pass tolerances or a convergence-order proof. Both controller and plant clocks change, so this test does not separate their causes.

Both coarse pulse trajectories replay 31,916 complete controller/plant records with zero mismatch in fresh instances. All four fine runs complete; both fine nominal pairings pass. All raw file hashes and unchanged runtime source hashes were checked after completion. No physical qualification, final holdout, new control law or parameter promotion follows.
