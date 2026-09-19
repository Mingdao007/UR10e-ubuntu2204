# Measured offline mechanism results

Legacy shared loop Kz=40 N/m

Simulation only. No physical result, formal holdout or declared winner.

Force peak is actual model contact load, not force-error peak. Progress is measured signed TCP motion projected onto the reference tangent; it is not an independent completion detector.

| Method | Material | Scenario | Force MAE N | Peak N | Path RMS mm | Progress ratio | Saturation % | QP intervention % | Full cycle |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| SFC | stiff_low_mu | nominal | 0.0627 | 5.5777 | 3.586 | 0.9899 | 0.00 | 20.94 | True |
| SFC | stiff_low_mu | sustained_release_normal | 0.5984 | 5.5777 | 3.607 | 0.9891 | 0.00 | 20.94 | True |
| SFC | stiff_low_mu | sustained_release_tangent | 0.1637 | 5.6956 | 3.617 | 0.9394 | 0.00 | 46.71 | True |
| SFC | stiff_low_mu | short_pulse_oblique | 0.1770 | 8.4319 | 3.247 | 0.9864 | 0.90 | 24.07 | True |
| SFC | compliant_high_mu | nominal | 0.1260 | 5.7321 | 3.857 | 0.9649 | 0.00 | 55.46 | True |
| SFC | compliant_high_mu | sustained_release_normal | 0.6297 | 5.7321 | 3.130 | 0.9502 | 1.86 | 60.38 | True |
| SFC | compliant_high_mu | sustained_release_tangent | 0.2132 | 5.7321 | 3.494 | 0.9229 | 1.55 | 71.38 | True |
| SFC | compliant_high_mu | short_pulse_oblique | 0.2293 | 6.4951 | 3.697 | 0.9615 | 3.07 | 57.85 | True |
| SFC_RADIAL | stiff_low_mu | nominal | 0.0506 | 5.5739 | 3.603 | 0.9896 | 0.00 | 21.32 | True |
| SFC_RADIAL | stiff_low_mu | sustained_release_normal | 0.5847 | 5.5739 | 3.624 | 0.9887 | 0.00 | 21.32 | True |
| SFC_RADIAL | stiff_low_mu | sustained_release_tangent | 0.1417 | 5.6009 | 3.596 | 0.9385 | 0.00 | 46.65 | True |
| SFC_RADIAL | stiff_low_mu | short_pulse_oblique | 0.1607 | 8.3339 | 3.191 | 0.9853 | 0.79 | 25.03 | True |
| SFC_RADIAL | compliant_high_mu | nominal | 0.1080 | 5.6813 | 3.693 | 0.9644 | 0.00 | 53.87 | True |
| SFC_RADIAL | compliant_high_mu | sustained_release_normal | 0.6208 | 5.6813 | 2.878 | 0.9646 | 1.59 | 44.88 | True |
| SFC_RADIAL | compliant_high_mu | sustained_release_tangent | 0.1710 | 5.6813 | 3.411 | 0.9230 | 0.00 | 69.23 | True |
| SFC_RADIAL | compliant_high_mu | short_pulse_oblique | 0.1900 | 6.4810 | 3.610 | 0.9602 | 1.96 | 49.88 | True |
| DSFC | stiff_low_mu | nominal | 0.0486 | 5.6207 | 3.538 | 0.9953 | 0.13 | 13.29 | True |
| DSFC | stiff_low_mu | sustained_release_normal | 0.5675 | 5.6207 | 3.553 | 0.9944 | 0.13 | 13.29 | True |
| DSFC | stiff_low_mu | sustained_release_tangent | 0.1428 | 5.6479 | 4.112 | 0.9433 | 0.86 | 39.92 | True |
| DSFC | stiff_low_mu | short_pulse_oblique | 0.1493 | 8.5331 | 3.078 | 0.9909 | 1.33 | 17.17 | True |
| DSFC | compliant_high_mu | nominal | 0.1241 | 5.6942 | 3.698 | 0.9724 | 0.00 | 36.83 | True |
| DSFC | compliant_high_mu | sustained_release_normal | 0.6088 | 5.6942 | 3.292 | 0.9593 | 2.63 | 48.05 | True |
| DSFC | compliant_high_mu | sustained_release_tangent | 0.1618 | 5.6942 | 3.172 | 0.9283 | 1.85 | 63.30 | True |
| DSFC | compliant_high_mu | short_pulse_oblique | 0.1951 | 6.6175 | 3.495 | 0.9708 | 3.78 | 37.16 | True |
| MSFC | stiff_low_mu | nominal | 0.0622 | 5.7737 | 2.514 | 1.0029 | 3.08 | 8.12 | True |
| MSFC | stiff_low_mu | sustained_release_normal | 0.7380 | 6.4145 | 2.558 | 1.0023 | 12.35 | 8.12 | True |
| MSFC | stiff_low_mu | sustained_release_tangent | 0.1546 | 5.7737 | 3.610 | 0.9576 | 5.36 | 28.92 | True |
| MSFC | stiff_low_mu | short_pulse_oblique | 0.4191 | 8.1377 | 1.756 | 0.9939 | 14.83 | 15.86 | True |
| MSFC | compliant_high_mu | nominal | 0.1010 | 5.3919 | 2.548 | 0.9756 | 2.94 | 39.95 | True |
| MSFC | compliant_high_mu | sustained_release_normal | 0.5968 | 5.3919 | 2.510 | 0.9770 | 7.94 | 38.61 | True |
| MSFC | compliant_high_mu | sustained_release_tangent | 0.1928 | 5.5249 | 4.247 | 0.9440 | 11.66 | 46.64 | True |
| MSFC | compliant_high_mu | short_pulse_oblique | 0.1517 | 6.5521 | 2.382 | 0.9938 | 11.98 | 11.95 | True |

## Matched nominal versus intervention

Recovery must occur after release and remain within 2 mm and 0.5 N of the same-method nominal through the remaining observation window (at least 0.1 s). Censored is not zero recovery time.

| Method | Material | Scenario | Yield peak mm | Recoil mm | Residual mm | Recovery s |
|---|---|---|---:|---:|---:|---:|
| SFC | stiff_low_mu | sustained_release_normal | 0.800 | 0.271 | 0.193 | 0.878 |
| SFC | stiff_low_mu | sustained_release_tangent | 9.960 | 0.000 | 6.589 | censored |
| SFC | stiff_low_mu | short_pulse_oblique | 0.878 | 1.104 | 0.556 | 8.202 |
| SFC | compliant_high_mu | sustained_release_normal | 2.711 | 0.861 | 0.648 | 25.818 |
| SFC | compliant_high_mu | sustained_release_tangent | 14.027 | 0.000 | 4.396 | censored |
| SFC | compliant_high_mu | short_pulse_oblique | 1.185 | 1.139 | 0.635 | 17.316 |
| SFC_RADIAL | stiff_low_mu | sustained_release_normal | 0.815 | 0.330 | 0.192 | 0.510 |
| SFC_RADIAL | stiff_low_mu | sustained_release_tangent | 9.618 | 0.000 | 6.520 | censored |
| SFC_RADIAL | stiff_low_mu | short_pulse_oblique | 0.857 | 1.161 | 0.674 | 8.250 |
| SFC_RADIAL | compliant_high_mu | sustained_release_normal | 3.304 | 1.106 | 4.443 | censored |
| SFC_RADIAL | compliant_high_mu | sustained_release_tangent | 13.922 | 0.000 | 4.835 | censored |
| SFC_RADIAL | compliant_high_mu | short_pulse_oblique | 1.063 | 1.771 | 2.920 | censored |
| DSFC | stiff_low_mu | sustained_release_normal | 0.857 | 0.505 | 0.233 | 0.000 |
| DSFC | stiff_low_mu | sustained_release_tangent | 10.200 | 0.000 | 6.948 | censored |
| DSFC | stiff_low_mu | short_pulse_oblique | 0.929 | 1.224 | 0.680 | 7.414 |
| DSFC | compliant_high_mu | sustained_release_normal | 2.497 | 3.411 | 2.752 | censored |
| DSFC | compliant_high_mu | sustained_release_tangent | 12.127 | 0.100 | 4.994 | censored |
| DSFC | compliant_high_mu | short_pulse_oblique | 1.008 | 0.909 | 0.809 | 34.594 |
| MSFC | stiff_low_mu | sustained_release_normal | 0.770 | 0.313 | 0.222 | 13.396 |
| MSFC | stiff_low_mu | sustained_release_tangent | 10.157 | 0.000 | 5.438 | censored |
| MSFC | stiff_low_mu | short_pulse_oblique | 1.516 | 2.159 | 1.480 | 28.228 |
| MSFC | compliant_high_mu | sustained_release_normal | 3.935 | 0.809 | 0.671 | 0.000 |
| MSFC | compliant_high_mu | sustained_release_tangent | 12.584 | 0.000 | 4.389 | censored |
| MSFC | compliant_high_mu | short_pulse_oblique | 1.825 | 2.043 | 2.542 | censored |

## Plant integration sensitivity at fixed 2 ms control period

| Method | Substeps | Max force difference from previous N | Max position difference mm |
|---|---:|---:|---:|
| SFC | 2 | 0.547428 | 0.131314 |
| SFC | 4 | 0.027045 | 0.031531 |
| SFC | 8 | 0.057165 | 0.008777 |
| DSFC | 2 | 0.423799 | 0.197390 |
| DSFC | 4 | 0.052488 | 0.033406 |
| DSFC | 8 | 0.017937 | 0.004927 |
| MSFC | 2 | 1.175814 | 0.274669 |
| MSFC | 4 | 0.137226 | 0.054040 |
| MSFC | 8 | 0.061326 | 0.008034 |

This sensitivity study has a short 0.6 s PATH horizon. It does not establish full-cycle ranking stability or hardware fidelity.

## Artifact locations

Raw immutable receipts: `/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/runs/yield-mechanisms-v2`.

Integration receipts: `/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/runs/yield-plant-refinement`.

The copied manifest contains SHA-256 for each complete compressed full-state run. Controller/native binary/plant identities and source hashes are embedded in each receipt.
