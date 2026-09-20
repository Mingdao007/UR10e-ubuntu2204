# FM-v1: mechanically matched MSFC metric ablation

Only minimum_metric_eigenvalue differs. Identity-metric eigenvalues are checked directly over every stored tick; force-history/filter states are retained. This is not a claim that all memory states are disabled.

| Scenario | Metric | Force MAE N | Peak N | Path RMS mm | Progress | Yield mm | Recovery s |
|---|---|---:|---:|---:|---:|---:|---:|
| nominal | identity_metric | 0.040168 | 5.821789 | 5.032552 | 0.872588 | NA | NA |
| nominal | on | 0.040821 | 5.833322 | 5.032553 | 0.872577 | NA | NA |
| sustained_release_tangent | identity_metric | 0.047249 | 5.821789 | 8.154662 | 0.888218 | 21.899204 | 1.008000 |
| sustained_release_tangent | on | 0.048520 | 5.833322 | 8.165330 | 0.888446 | 21.930433 | 1.008000 |
