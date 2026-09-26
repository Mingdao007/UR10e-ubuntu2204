# TASE integral anti-windup screening evidence — 2026-09-27

## Finding

The existing D arm did exercise conditional integration and final-output feedback; its anti-windup path was not dormant. It still did not improve the A baseline in the completed screening. Keep A (`1.0 N·s`, incumbent Md/Bd) as the reference; do not lower the limit or promote D from these results.

## What D did

D used the recorded conditional double-clamp policy: state cap 1.0 N·s plus 0.5 N I-term authority, with same-direction integration frozen when a published normal task-space limit prevented the requested velocity from being realized; reverse-error unwinding remained allowed. Across the five D attempts:

- `integral_conditional_frozen`: 4,724–6,474 ticks, 15.8%–21.6% of recorded ticks.
- `integral_final_output_frozen`: 4,627–6,319 ticks, 15.5%–21.1%.
- `integral_velocity_saturated` and `published_task_space_saturated` counts match to within one tick (4,626–6,317), confirming the final-output feedback path was active at the normal task-space limiter.
- I-term authority clamp occurred on 606–1,776 ticks (2.2%–5.9%).

This separates D's genuine anti-windup events from the misleading broad `integral_explicit_velocity_limit_active` field documented in the [flag semantics note](tase-saturation-flag-semantics-20260927.md).

## D versus A

Across the five randomized screening blocks, every D-minus-A formal-window MAE diagnostic was positive: **+0.231 to +0.254 N**, mean **+0.244 N** (higher is worse). Two of these five block pairs included an attempt that failed formal admission; their MAE values remain diagnostic only. Among admitted units, A and D each had 4/5 complete attempts; mean formal MAE was **0.977 N for A** and **1.235 N for D**. This was a screening set, not a confirmatory significance test, so it does not establish that conditional anti-windup is generally harmful.

Interpretation: the present D policy successfully prevents further same-direction integration during normal task-space clipping, yet its smaller effective I authority did not improve this trajectory's force score. The existing data do not justify another live repeat of the same D profile. A different anti-windup candidate should first be motivated by a diagnosed error episode and checked against the recorded output/saturation traces offline.

Sources: [A–E reducer](tase-r31-integral-screening-r013-diagnostic-20260927.json), [stage metric report](tase-force-metric-alignment-20260927.md), and `tools/step5d_autotune_v4_r004/calibrated_runtime.py`.
