# NO-v3 step refinement

Fixed parameters, same two across-prior nominal cells. Controller period and plant substep both halve; this is a sensitivity check, not independent physics validation.

| Observer | dt ms | Normal RMS deg | Force MAE N | Peak N | Path RMS mm | Progress | QP ticks |
|---|---:|---:|---:|---:|---:|---:|---:|
| combined-mild-across | 2 | 2.899 | 0.087 | 6.268 | 5.368 | 0.857 | 0 |
| combined-mild-across | 1 | 3.130 | 0.101 | 6.302 | 5.402 | 0.853 | 0 |
| motion-only-mild-across | 2 | 7.645 | 0.096 | 6.266 | 6.697 | 0.890 | 44 |
| motion-only-mild-across | 1 | 7.685 | 0.103 | 6.301 | 6.699 | 0.885 | 121 |

The combined observer retains the normal/path-error benefit and lower measured progress relative to motion-only at 1 ms. Neither setting fails or loses contact. Across common sample times, maximum position differences are 0.442 mm (combined) and 0.490 mm (motion-only); maximum force differences are 0.195 N and 0.119 N. The trajectories are numerically sensitive, so this is qualitative persistence of the observed tradeoff, not a certified convergence order or tolerance pass. No physical acceptance follows.
