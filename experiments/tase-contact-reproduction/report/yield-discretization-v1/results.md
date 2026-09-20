# DC-v1: separate control and plant discretization

All four cells use the same SFC parameters, NO-v3 observer and normal intervention. Two cells are retained and two are new.

| Control dt ms | Plant step ms | Min load N | Load <1 N s | Zero load s | Geometric separation s | Force MAE N | Peak N | Path RMS mm | Progress | Saturation s | QP intervention s |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2.0000 | 0.2500 | 0.8425 | 0.4340 | 0.0000 | 0.0000 | 0.7219 | 6.2229 | 4.9025 | 0.8888 | 0.0000 | 0.2480 |
| 2.0000 | 0.1250 | 0.8221 | 0.4940 | 0.0000 | 0.0000 | 0.7261 | 6.2374 | 4.8946 | 0.8898 | 0.0000 | 0.2540 |
| 1.0000 | 0.2500 | 0.6661 | 0.9350 | 0.0000 | 0.0000 | 0.7636 | 6.3474 | 4.8332 | 0.8969 | 0.2490 | 0.3020 |
| 1.0000 | 0.1250 | 0.6427 | 0.9850 | 0.0000 | 0.0000 | 0.7693 | 6.3588 | 4.8328 | 0.8978 | 0.3240 | 0.3140 |

`contact_loss_duration_s` is the unchanged historical <1 N threshold metric. It is not physical separation. Zero force and nonnegative surface gap are separately evaluated. Saturation and QP exposure are converted from ticks to seconds to compare grids. All failures and original metrics remain in results.json. Two levels do not establish convergence order; no controller ranking or physical acceptance follows.
