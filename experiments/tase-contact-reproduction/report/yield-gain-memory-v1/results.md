# GM-v1 measured results

Development-only factorial ablation. Beta=1 removes mechanical memory coupling; it does not reset state.

| Variant | Scenario | Plant substeps | Force MAE N | Peak N | Path RMS mm | Progress | Tail force std N | Tail dominant Hz |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| MSFC-GM-v1-g100-on | nominal | 4 | 0.095889 | 5.980251 | 1.0298 | 1.022746 | 0.004490 | 0.100 |
| MSFC-GM-v1-g100-on | nominal | 8 | 0.101478 | 5.988215 | 1.0303 | 1.022761 | 0.004605 | 0.100 |
| MSFC-GM-v1-g100-on | sustained_release_normal | 4 | 0.848948 | 6.930535 | 1.0578 | 1.022095 | 0.249647 | 2.300 |
| MSFC-GM-v1-g100-on | sustained_release_normal | 8 | 0.846315 | 6.952759 | 1.0588 | 1.022014 | 0.189441 | 2.300 |
| MSFC-GM-v1-g100-identity_metric | nominal | 4 | 0.095254 | 5.979355 | 1.0296 | 1.022747 | 0.004491 | 0.100 |
| MSFC-GM-v1-g100-identity_metric | nominal | 8 | 0.101500 | 5.987334 | 1.0301 | 1.022761 | 0.004624 | 0.100 |
| MSFC-GM-v1-g100-identity_metric | sustained_release_normal | 4 | 0.844451 | 6.924302 | 1.0575 | 1.022061 | 0.224750 | 2.300 |
| MSFC-GM-v1-g100-identity_metric | sustained_release_normal | 8 | 0.844606 | 6.945853 | 1.0585 | 1.022042 | 0.184067 | 2.300 |
| MSFC-GM-v1-g50-on | nominal | 4 | 0.061173 | 5.756826 | 1.1622 | 1.021878 | 0.004507 | 0.100 |
| MSFC-GM-v1-g50-on | nominal | 8 | 0.061478 | 5.761851 | 1.1625 | 1.021883 | 0.004507 | 0.100 |
| MSFC-GM-v1-g50-on | sustained_release_normal | 4 | 0.588291 | 5.756826 | 1.1807 | 1.021226 | 0.004495 | 0.100 |
| MSFC-GM-v1-g50-on | sustained_release_normal | 8 | 0.591855 | 5.761851 | 1.1810 | 1.021228 | 0.004497 | 0.100 |
| MSFC-GM-v1-g50-identity_metric | nominal | 4 | 0.061073 | 5.754068 | 1.1656 | 1.021873 | 0.004507 | 0.100 |
| MSFC-GM-v1-g50-identity_metric | nominal | 8 | 0.061337 | 5.759053 | 1.1659 | 1.021879 | 0.004507 | 0.100 |
| MSFC-GM-v1-g50-identity_metric | sustained_release_normal | 4 | 0.587930 | 5.754068 | 1.1841 | 1.021221 | 0.004495 | 0.100 |
| MSFC-GM-v1-g50-identity_metric | sustained_release_normal | 8 | 0.591438 | 5.759053 | 1.1844 | 1.021224 | 0.004497 | 0.100 |

## Unaligned refinement

| Variant | Scenario | Max force difference N | Max TCP difference mm |
|---|---|---:|---:|
| MSFC-GM-v1-g100-on | nominal | 0.090605 | 0.018414 |
| MSFC-GM-v1-g100-on | sustained_release_normal | 3.602706 | 0.672130 |
| MSFC-GM-v1-g100-identity_metric | nominal | 0.089595 | 0.022674 |
| MSFC-GM-v1-g100-identity_metric | sustained_release_normal | 3.548191 | 0.660232 |
| MSFC-GM-v1-g50-on | nominal | 0.008232 | 0.005237 |
| MSFC-GM-v1-g50-on | sustained_release_normal | 0.043874 | 0.012494 |
| MSFC-GM-v1-g50-identity_metric | nominal | 0.012087 | 0.006836 |
| MSFC-GM-v1-g50-identity_metric | sustained_release_normal | 0.044632 | 0.013129 |

## Matched nominal/release (8 substeps)

| Variant | Recovery s | Recoil mm | Residual mm |
|---|---:|---:|---:|
| MSFC-GM-v1-g100-on | 16.3400 | 0.1709 | 0.0693 |
| MSFC-GM-v1-g100-identity_metric | 16.4700 | 0.1785 | 0.0583 |
| MSFC-GM-v1-g50-on | 0.1400 | 0.2028 | 0.0002 |
| MSFC-GM-v1-g50-identity_metric | 0.1360 | 0.2026 | 0.0002 |
