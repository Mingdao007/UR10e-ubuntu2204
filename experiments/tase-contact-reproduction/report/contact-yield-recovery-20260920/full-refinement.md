# Full-cycle integration sensitivity

Fixed 2 ms control period, plant 0.5 ms versus 0.25 ms, same parameters. No retuning.

| Method | Scenario | Max force difference N | Max TCP difference mm | Force MAE at 0.25 ms N | Peak at 0.25 ms N |
|---|---|---:|---:|---:|---:|
| SFC | nominal | 0.01581 | 0.00639 | 0.05865 | 5.46739 |
| SFC | sustained_release_normal | 0.02744 | 0.00847 | 0.61107 | 5.63366 |
| DSFC | nominal | 0.00586 | 0.00443 | 0.05876 | 5.53316 |
| DSFC | sustained_release_normal | 0.04328 | 0.01158 | 0.59158 | 5.54535 |
| MSFC | nominal | 0.49346 | 0.13734 | 0.13756 | 6.11164 |
| MSFC | sustained_release_normal | 3.51611 | 0.47087 | 0.84339 | 6.95810 |

Pointwise MSFC discrepancies remain large over the full cycle. Similar aggregate metrics do not certify trajectory convergence. This may involve phase sensitivity in the current delayed/saturated closed loop; it does not identify memory dynamics alone as the cause.

Raw receipts: `/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/runs/yield-full-refinement`.
