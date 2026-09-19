# SE-v1: surface variation with unchanged control task

Four new full-duration attempts, fixed DSFC candidate and two existing observer modes, plus four retained mild-surface comparators. Curvature changes from (0.8, 0.4) to (6, 8) inverse metres; path, initial approach, initial contact height/load construction and controller parameters remain fixed. The observer sees measured velocity/force only. No tuning or new observer is introduced.

The fixed reference visits planned true-normal deviations of 10.108 degrees RMS and 13.521 degrees maximum from the approach. These are evaluator diagnostics, never controller inputs. Actual visited normal variation is separately recorded in results.json, since poor physical progress could otherwise mimic good estimation.

| Surface | Material | Observer | Full cycle | Failed | Normal RMS deg | Attitude RMS deg | Force MAE N | Peak N | Path RMS mm | Progress | Contact loss s |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| mild retained | stiff_low_mu | legacy | True | False | 6.707 | 6.494 | 0.067 | 5.546 | 1.340 | 1.022 | 0.000 |
| mild retained | compliant_high_mu | legacy | True | False | 17.923 | 13.552 | 0.277 | 5.698 | 2.223 | 1.059 | 0.000 |
| mild retained | stiff_low_mu | frozen_prior | True | False | 1.357 | 1.355 | 0.050 | 6.066 | 5.044 | 0.872 | 0.000 |
| mild retained | compliant_high_mu | frozen_prior | True | False | 1.286 | 1.281 | 0.061 | 5.965 | 11.834 | 0.610 | 0.000 |
| strong SE-v1 | stiff_low_mu | legacy | True | False | 6.045 | 5.144 | 0.056 | 5.549 | 1.484 | 0.999 | 0.000 |
| strong SE-v1 | stiff_low_mu | frozen_prior | True | False | 7.931 | 7.928 | 0.117 | 6.072 | 7.242 | 0.723 | 0.000 |
| strong SE-v1 | compliant_high_mu | legacy | True | False | 16.923 | 8.767 | 0.268 | 5.701 | 2.665 | 1.015 | 0.000 |
| strong SE-v1 | compliant_high_mu | frozen_prior | True | False | 7.226 | 7.219 | 0.148 | 5.968 | 12.468 | 0.531 | 0.000 |

Full cycle means complete scheduled time coverage, not successful path execution. All data are development. A reduced force peak with worse attitude/path/progress is not a winner. Raw failure messages and every metric are retained in results.json. Physics, force-limit interpretation, robot speed clipping and sensor/servo fidelity remain unqualified. This nominal-only comparison cannot establish transverse-intervention robustness.
