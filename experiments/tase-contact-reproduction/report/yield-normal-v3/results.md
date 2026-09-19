# NO-v3 combined observer: frozen development comparison

Nine new DSFC cells, one material, fixed gains. Four prior/curvature comparators are retained with verified hashes. All results are development; no holdout, physical qualification, or single-metric winner.

| Cell | Full | Failed | Normal RMS deg | Attitude RMS deg | Force MAE N | Peak N | Path RMS mm | Progress | Contact loss s | Recovery s |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| combined-mild-approach | True | False | 1.156 | 1.162 | 0.071 | 5.886 | 5.219 | 0.875 | 0.000 | NA |
| combined-mild-along | True | False | 1.647 | 1.126 | 0.041 | 5.407 | 4.898 | 0.883 | 0.000 | NA |
| combined-mild-across | True | False | 2.899 | 2.690 | 0.087 | 6.268 | 5.368 | 0.857 | 0.000 | NA |
| combined-strong-approach | True | False | 5.950 | 6.015 | 0.115 | 5.987 | 7.026 | 0.745 | 0.000 | NA |
| combined-normal-hold | True | False | 1.465 | 1.457 | 0.637 | 5.922 | 4.678 | 0.888 | 0.000 | 3.834 |
| combined-tangent-hold | True | False | 5.678 | 4.875 | 0.128 | 5.886 | 8.121 | 0.900 | 0.000 | 8.424 |
| motion-only-mild-across | True | False | 7.645 | 7.539 | 0.096 | 6.266 | 6.697 | 0.890 | 0.000 | NA |
| frozen-normal-hold | True | False | 1.346 | 1.344 | 0.578 | 6.066 | 4.708 | 0.877 | 0.000 | 0.000 |
| frozen-tangent-hold | True | False | 1.261 | 1.262 | 0.065 | 6.066 | 8.261 | 0.889 | 0.000 | 0.942 |
| retained-approach | True | False | 1.357 | 1.355 | 0.050 | 6.066 | 5.044 | 0.872 | 0.000 | NA |
| retained-along_10deg | True | False | 9.380 | 9.122 | 0.114 | 5.392 | 8.913 | 0.900 | 0.000 | NA |
| retained-across_10deg | True | False | 9.700 | 9.592 | 0.120 | 6.227 | 8.347 | 0.870 | 0.000 | NA |
| retained-strong-frozen | True | False | 7.931 | 7.928 | 0.117 | 6.072 | 7.242 | 0.723 | 0.000 | NA |

Recovery uses the existing matched nominal definition, with right censoring retained. Full cycle denotes scheduled time coverage, not successful path completion. Initial-state and PATH-start normal errors, gate fractions, tail errors and all failure messages are in results.json. The coplanarity term assumes the measured force is in the normal/actual-slide plane; transverse external force can violate that condition. The ideal Coulomb simulator cannot establish noise, stick-slip or physical intervention robustness.
