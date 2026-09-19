# TR-v1 development results

Frozen parameters; common outer and constraints; no retuning or final holdout.
All scenarios are retained, including failures. Progress is measured projection, not a completion certificate.

| Variant | Material | Scenario | Force MAE N | Peak N | Path RMS mm | Orientation RMS deg | Progress | Contact loss s | Recovery s |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| SFC | stiff_low_mu | nominal | 0.0731 | 5.4444 | 1.4702 | 6.4852 | 1.0229 | 0.0000 | - |
| SFC | stiff_low_mu | sustained_release_normal | 0.6152 | 5.6352 | 1.4903 | 5.8294 | 1.0219 | 0.0000 | 1.1760 |
| SFC | stiff_low_mu | sustained_release_tangent | 0.2030 | 5.8899 | 2.4119 | 6.7203 | 1.0232 | 0.0000 | 10.9260 |
| SFC | stiff_low_mu | short_pulse_oblique | 0.2183 | 8.1109 | 1.5081 | 6.4077 | 1.0225 | 0.0000 | 10.8820 |
| SFC | compliant_high_mu | nominal | 0.2858 | 5.7628 | 2.3804 | 13.4918 | 1.0607 | 0.0000 | - |
| SFC | compliant_high_mu | sustained_release_normal | 0.7557 | 5.7628 | 2.6113 | 12.3837 | 1.0589 | 0.0000 | 5.9020 |
| SFC | compliant_high_mu | sustained_release_tangent | 0.2671 | 5.7628 | 3.1520 | 9.8860 | 1.0602 | 0.0000 | 0.0840 |
| SFC | compliant_high_mu | short_pulse_oblique | 0.3320 | 6.1976 | 2.3324 | 13.6507 | 1.0597 | 0.0000 | 9.9300 |
| DSFC | stiff_low_mu | nominal | 0.0666 | 5.5459 | 1.3396 | 6.4942 | 1.0223 | 0.0000 | - |
| DSFC | stiff_low_mu | sustained_release_normal | 0.5972 | 5.5650 | 1.3584 | 5.8124 | 1.0216 | 0.0000 | 0.8980 |
| DSFC | stiff_low_mu | sustained_release_tangent | 0.1620 | 5.9473 | 2.2294 | 6.6064 | 1.0194 | 0.0000 | 2.6600 |
| DSFC | stiff_low_mu | short_pulse_oblique | 0.2016 | 8.1701 | 1.3638 | 6.4104 | 1.0219 | 0.0000 | 9.4600 |
| DSFC | compliant_high_mu | nominal | 0.2770 | 5.6976 | 2.2227 | 13.5520 | 1.0591 | 0.0000 | - |
| DSFC | compliant_high_mu | sustained_release_normal | 0.7421 | 5.6976 | 2.3337 | 12.6413 | 1.0560 | 0.0000 | 1.7620 |
| DSFC | compliant_high_mu | sustained_release_tangent | 0.2542 | 5.6976 | 2.9891 | 10.0839 | 1.0567 | 0.0000 | 0.5660 |
| DSFC | compliant_high_mu | short_pulse_oblique | 0.3281 | 6.3120 | 2.1799 | 13.6809 | 1.0577 | 0.0000 | 11.9700 |
| MSFC-GM-v1-g50-on | stiff_low_mu | nominal | 0.0615 | 5.7619 | 1.1625 | 6.5573 | 1.0219 | 0.0000 | - |
| MSFC-GM-v1-g50-on | stiff_low_mu | sustained_release_normal | 0.5919 | 5.7619 | 1.1810 | 5.8701 | 1.0212 | 0.0000 | 0.1400 |
| MSFC-GM-v1-g50-on | stiff_low_mu | sustained_release_tangent | 0.1343 | 5.7651 | 1.9172 | 6.6795 | 1.0175 | 0.0000 | 0.4600 |
| MSFC-GM-v1-g50-on | stiff_low_mu | short_pulse_oblique | 0.1715 | 7.8891 | 1.1802 | 6.4597 | 1.0211 | 0.0000 | 8.8300 |
| MSFC-GM-v1-g50-on | compliant_high_mu | nominal | 0.2773 | 5.5469 | 2.1748 | 13.5665 | 1.0543 | 0.0000 | - |
| MSFC-GM-v1-g50-on | compliant_high_mu | sustained_release_normal | 0.7411 | 5.5469 | 2.1854 | 12.9814 | 1.0515 | 0.0000 | 0.0000 |
| MSFC-GM-v1-g50-on | compliant_high_mu | sustained_release_tangent | 0.2452 | 5.5469 | 2.7936 | 10.2903 | 1.0514 | 0.0000 | 0.5040 |
| MSFC-GM-v1-g50-on | compliant_high_mu | short_pulse_oblique | 0.3085 | 6.0580 | 2.1491 | 13.6718 | 1.0529 | 0.0000 | 2.5860 |
| MSFC-GM-v1-g50-identity_metric | stiff_low_mu | nominal | 0.0613 | 5.7591 | 1.1659 | 6.5578 | 1.0219 | 0.0000 | - |
| MSFC-GM-v1-g50-identity_metric | stiff_low_mu | sustained_release_normal | 0.5914 | 5.7591 | 1.1844 | 5.8708 | 1.0212 | 0.0000 | 0.1360 |
| MSFC-GM-v1-g50-identity_metric | stiff_low_mu | sustained_release_tangent | 0.1333 | 5.7601 | 1.9041 | 6.6900 | 1.0174 | 0.0000 | 0.4560 |
| MSFC-GM-v1-g50-identity_metric | stiff_low_mu | short_pulse_oblique | 0.1675 | 7.8082 | 1.1847 | 6.4619 | 1.0211 | 0.0000 | 8.7900 |
| MSFC-GM-v1-g50-identity_metric | compliant_high_mu | nominal | 0.2775 | 5.5395 | 2.1822 | 13.5632 | 1.0543 | 0.0000 | - |
| MSFC-GM-v1-g50-identity_metric | compliant_high_mu | sustained_release_normal | 0.7413 | 5.5395 | 2.1911 | 12.9806 | 1.0515 | 0.0000 | 0.0000 |
| MSFC-GM-v1-g50-identity_metric | compliant_high_mu | sustained_release_tangent | 0.2443 | 5.5395 | 2.7768 | 10.2919 | 1.0513 | 0.0000 | 0.5100 |
| MSFC-GM-v1-g50-identity_metric | compliant_high_mu | short_pulse_oblique | 0.3070 | 6.0075 | 2.1570 | 13.6687 | 1.0529 | 0.0000 | 3.5960 |

## Mechanically matched memory ablation

All deltas are ON minus identity metric. Negative is lower, not automatically better yielding.

| Material | Scenario | Delta peak N | Delta path RMS mm | Delta recovery s | Delta yielding mm |
|---|---|---:|---:|---:|---:|
| stiff_low_mu | nominal | 0.0028 | -0.0035 | - | - |
| stiff_low_mu | sustained_release_normal | 0.0028 | -0.0034 | 0.0040 | 0.0013 |
| stiff_low_mu | sustained_release_tangent | 0.0049 | 0.0131 | 0.0040 | 0.1547 |
| stiff_low_mu | short_pulse_oblique | 0.0809 | -0.0045 | 0.0400 | 0.0147 |
| compliant_high_mu | nominal | 0.0074 | -0.0073 | - | - |
| compliant_high_mu | sustained_release_normal | 0.0074 | -0.0057 | 0.0000 | 0.0274 |
| compliant_high_mu | sustained_release_tangent | 0.0074 | 0.0169 | -0.0060 | 0.1940 |
| compliant_high_mu | short_pulse_oblique | 0.0504 | -0.0079 | -1.0100 | 0.0229 |
