# Corrected shared-loop numerical check

Kz=0, 2 ms control; 0.5 versus 0.25 ms plant integration. Frozen law parameters.

| Method | Scenario | Max force difference N | Max TCP difference mm |
|---|---|---:|---:|
| SFC | nominal | 0.012117 | 0.004599 |
| SFC | sustained_release_tangent | 0.036016 | 0.017081 |
| DSFC | nominal | 0.009433 | 0.005869 |
| DSFC | sustained_release_tangent | 0.009433 | 0.006631 |
| MSFC | nominal | 0.090605 | 0.018414 |
| MSFC | sustained_release_tangent | 0.090605 | 0.019502 |
| MSFC | sustained_release_normal | 3.602706 | 0.672130 |

**Pilot advancement: NOT PASSED.** MSFC sustained normal loading remains highly sensitive in this closed-loop configuration. The tangent-recovery improvement cannot qualify the entire constant-force / unknown-surface / orientation task.

This does not isolate the cause to memory alone. Native gain, delayed force filtering, velocity saturation, model integration and memory must be separated before a real-platform pilot.

Raw receipts: `/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/runs/yield-common-fix-qualification`.
