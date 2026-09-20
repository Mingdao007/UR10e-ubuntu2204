# FT-v1: frozen-observer ablation across three control dynamics

Four new SFC/MSFC trials plus two reused DSFC trials, with the 12 legacy/NO-v3 references from OT-v1. No parameter tuning; the frozen observer is not an unknown-surface solution.

| Method | Observer | Scenario | Failed | Force MAE N | Peak N | Path RMS mm | Progress | Load <1 N s | Yield mm | Recovery s |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| DSFC | NO-v3 | nominal | False | 0.071 | 5.886 | 5.219 | 0.875 | 0.000 | NA | NA |
| DSFC | frozen | nominal | False | 0.050 | 6.066 | 5.044 | 0.872 | 0.000 | NA | NA |
| DSFC | legacy | nominal | False | 0.067 | 5.546 | 1.340 | 1.022 | 0.000 | NA | NA |
| DSFC | NO-v3 | sustained_release_tangent | False | 0.128 | 5.886 | 8.121 | 0.900 | 0.000 | 22.025 | 8.424 |
| DSFC | frozen | sustained_release_tangent | False | 0.065 | 6.066 | 8.261 | 0.889 | 0.000 | 23.035 | 0.942 |
| DSFC | legacy | sustained_release_tangent | False | 0.162 | 5.947 | 2.229 | 1.019 | 0.000 | 5.831 | 2.660 |
| MSFC | NO-v3 | nominal | False | 0.061 | 5.726 | 5.217 | 0.873 | 0.000 | NA | NA |
| MSFC | frozen | nominal | False | 0.041 | 5.833 | 5.033 | 0.873 | 0.000 | NA | NA |
| MSFC | legacy | nominal | False | 0.061 | 5.762 | 1.162 | 1.022 | 0.000 | NA | NA |
| MSFC | NO-v3 | sustained_release_tangent | False | 0.108 | 5.726 | 8.072 | 0.896 | 0.000 | 20.572 | 8.630 |
| MSFC | frozen | sustained_release_tangent | False | 0.049 | 5.833 | 8.165 | 0.888 | 0.000 | 21.930 | 1.008 |
| MSFC | legacy | sustained_release_tangent | False | 0.134 | 5.765 | 1.917 | 1.017 | 0.000 | 4.608 | 0.460 |
| SFC | NO-v3 | nominal | False | 0.112 | 6.223 | 5.433 | 0.855 | 0.000 | NA | NA |
| SFC | frozen | nominal | False | 0.084 | 6.195 | 5.052 | 0.873 | 0.000 | NA | NA |
| SFC | legacy | nominal | False | 0.073 | 5.444 | 1.470 | 1.023 | 0.000 | NA | NA |
| SFC | NO-v3 | sustained_release_tangent | False | 0.217 | 6.820 | 8.408 | 0.883 | 0.000 | 20.181 | 8.848 |
| SFC | frozen | sustained_release_tangent | False | 0.169 | 6.234 | 8.264 | 0.892 | 0.000 | 23.387 | 1.004 |
| SFC | legacy | sustained_release_tangent | False | 0.203 | 5.890 | 2.412 | 1.023 | 0.000 | 6.240 | 10.926 |
