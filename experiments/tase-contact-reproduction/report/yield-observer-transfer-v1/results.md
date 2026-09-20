# OT-v1: common-observer transfer across the three fixed control dynamics

Six new SFC/MSFC trials and three retained DSFC NO-v3 trials, compared with nine hash-verified legacy-observer trials. Mechanical/settings/initial-state/reference invariant checks are retained in results.json; failed or shortened traces remain visible and cannot support matched ranking. No retuning, equal-budget optimization, holdout or final ranking.

| Method | Observer | Scenario | Failed | Normal RMS deg | Force MAE N | Peak N | Path RMS mm | Progress | Loss s | Recovery s |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| SFC | legacy | nominal | False | 6.710 | 0.073 | 5.444 | 1.470 | 1.023 | 0.000 | NA |
| SFC | NO-v3 | nominal | False | 2.560 | 0.112 | 6.223 | 5.433 | 0.855 | 0.000 | NA |
| SFC | legacy | sustained_release_normal | False | 6.048 | 0.615 | 5.635 | 1.490 | 1.022 | 0.000 | 1.176 |
| SFC | NO-v3 | sustained_release_normal | False | 3.205 | 0.722 | 6.223 | 4.903 | 0.889 | 0.434 | 20.384 |
| SFC | legacy | sustained_release_tangent | False | 11.363 | 0.203 | 5.890 | 2.412 | 1.023 | 0.000 | 10.926 |
| SFC | NO-v3 | sustained_release_tangent | False | 5.555 | 0.217 | 6.820 | 8.408 | 0.883 | 0.000 | 8.848 |
| DSFC | legacy | nominal | False | 6.707 | 0.067 | 5.546 | 1.340 | 1.022 | 0.000 | NA |
| DSFC | NO-v3 | nominal | False | 1.156 | 0.071 | 5.886 | 5.219 | 0.875 | 0.000 | NA |
| DSFC | legacy | sustained_release_normal | False | 6.029 | 0.597 | 5.565 | 1.358 | 1.022 | 0.000 | 0.898 |
| DSFC | NO-v3 | sustained_release_normal | False | 1.465 | 0.637 | 5.922 | 4.678 | 0.888 | 0.000 | 3.834 |
| DSFC | legacy | sustained_release_tangent | False | 11.388 | 0.162 | 5.947 | 2.229 | 1.019 | 0.000 | 2.660 |
| DSFC | NO-v3 | sustained_release_tangent | False | 5.678 | 0.128 | 5.886 | 8.121 | 0.900 | 0.000 | 8.424 |
| MSFC | legacy | nominal | False | 6.759 | 0.061 | 5.762 | 1.162 | 1.022 | 0.000 | NA |
| MSFC | NO-v3 | nominal | False | 1.299 | 0.061 | 5.726 | 5.217 | 0.873 | 0.000 | NA |
| MSFC | legacy | sustained_release_normal | False | 6.082 | 0.592 | 5.762 | 1.181 | 1.021 | 0.000 | 0.140 |
| MSFC | NO-v3 | sustained_release_normal | False | 1.616 | 0.603 | 5.726 | 4.733 | 0.880 | 0.000 | 1.122 |
| MSFC | legacy | sustained_release_tangent | False | 11.260 | 0.134 | 5.765 | 1.917 | 1.017 | 0.000 | 0.460 |
| MSFC | NO-v3 | sustained_release_tangent | False | 5.619 | 0.108 | 5.726 | 8.072 | 0.896 | 0.000 | 8.630 |
