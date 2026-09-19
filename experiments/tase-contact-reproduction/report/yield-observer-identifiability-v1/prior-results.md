# P0-v1: independent observer prior uncertainty

Six development-only nominal full-cycle trials, fixed DSFC candidate and shared plant/controller settings. The physical approach, robot initial configuration, surface, reference path, force target and orientation-compliance law are unchanged between priors. Only estimator initialization differs; motion_gain=0 and force_correction_gain=0 explicitly freeze its state.

The two 10-degree errors point respectively along the projected initial feed direction and across it, in the physical tangent plane. Their input is defined from the known approach and task reference, not simulator surface truth. Full initial and evolving state and raw observations are retained for every cell. No reinitialization during contact and no retuning.

| Material | Prior | Full cycle | Normal RMS deg | Attitude RMS deg | Force MAE N | Peak N | Path RMS mm | Progress ratio | Contact loss s |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| stiff_low_mu | approach | True | 1.357 | 1.355 | 0.050 | 6.066 | 5.044 | 0.872 | 0.000 |
| stiff_low_mu | along_10deg | True | 9.380 | 9.122 | 0.114 | 5.392 | 8.913 | 0.900 | 0.000 |
| stiff_low_mu | across_10deg | True | 9.700 | 9.592 | 0.120 | 6.227 | 8.347 | 0.870 | 0.000 |
| compliant_high_mu | approach | True | 1.286 | 1.281 | 0.061 | 5.965 | 11.834 | 0.610 | 0.000 |
| compliant_high_mu | along_10deg | True | 9.277 | 9.006 | 0.206 | 5.652 | 14.793 | 0.636 | 0.000 |
| compliant_high_mu | across_10deg | True | 9.673 | 9.556 | 0.153 | 5.910 | 13.451 | 0.612 | 0.000 |

This is a prior-sensitivity baseline, not a proposed solution for unknown surfaces. Small error from the approach prior cannot demonstrate online identification. Conversely a worse tilted-prior result does not prove that a particular adaptive estimator can correct it. All cells are development; none is an independent holdout. The nominal-only matrix cannot establish intervention robustness or large varying-normal tracking.

Full metrics (including saturation, QP intervention, force-limit duration and failure reasons) and raw receipt hashes are in prior-results.json. Force diagnostic limits are simulator reporting thresholds, not fragile-object damage limits. The model's joint-rate clip, friction and servo assumptions remain unchanged and unvalidated physically.
