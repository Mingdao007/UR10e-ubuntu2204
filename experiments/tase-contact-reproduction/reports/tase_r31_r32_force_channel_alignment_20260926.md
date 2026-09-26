# R31/R32 TASE-RNN A force-channel alignment — 2026-09-26

## Finding

Four complete, eligible rate460 A traces expose a repeatable difference between the normal-force signal used by the controller and the filtered normal-force signal used for scoring. The current provider sends `max(filtered_normal, measured_normal)` to the outer force loop. The scored diagnostic uses `filtered_normal` alone. Both come from the same Kunwei sensor stream, so this is a signal-processing mismatch, not a comparison to independent force truth.

Across the four traces, the controller input averaged **0.808–1.020 N above** the scored filter. The outer-loop signed error consequently averaged only **0.172–0.242 N**, while the scored-filter signal averaged **1.000–1.261 N below** the 5 N target. The unfiltered normal load exceeded the filter on **46.3–49.3%** of formal samples. This repeated relationship is a plausible contributor to the measured force error, but it does not establish that the physical contact force is low by the same amount.

## Per-attempt diagnostics

Official MAE is the 550-bin score. The sample MAEs below use all command samples in `[5,60)` and are diagnostic values with different aggregation.

| Unit | Official MAE (N) | Filtered sample MAE (N) | Controller-input sample MAE (N) | Controller input minus filter (N) | Normal command limited (%) | `Jqdot` normal residual p95 (m/s) |
|---|---:|---:|---:|---:|---:|---:|
| R31-A1 | 1.282720 | 1.286425 | 1.403209 | 1.019628 | 30.43 | 0.002921 |
| R31-A2 | 1.006797 | 1.007634 | 1.054458 | 0.807681 | 19.49 | 0.002815 |
| R31-A3 | 1.017505 | 1.019193 | 1.083302 | 0.833145 | 18.99 | 0.002751 |
| R32-A1 | 1.098445 | 1.099853 | 1.196105 | 0.882412 | 22.60 | 0.002810 |

The “controller input minus filter” column is the mean of `max(filtered_normal, measured_normal) - filtered_normal`. The “normal command limited” event marks rows where the requested normal component differs from the task-space command after the existing task limits. It can reflect the normal cap or the total linear cap, so it does not isolate one limit.

On those limited rows, the filtered-force sample MAE was **2.23–2.35 N**, compared with **0.69–0.86 N** on rows without that event. The association is expected to be partly self-triggered: a large force error asks for more normal velocity, which can then meet the existing limit. It does not show that raising that limit would improve stable closed-loop control.

The RNN's final joint command also does not consistently realize its task-normal request under the calibrated Jacobian. The p95 absolute difference between task command normal velocity and final published `Jqdot` normal velocity was **2.75–2.92 mm/s** across these A traces. This is a kinematic command residual calculated from the calibrated model, not a measured TCP velocity. The same runs passed the published-joint-velocity versus RTDE `actual_qd` gate, with correlations **0.968–0.972**. That combination says the robot tracks the smooth joint commands reasonably well while those commands can still under-realize the requested task-normal velocity.

The 1.0 N·s integral state reached its positive cap on **7.03–10.32%** of formal rows. That shows occasional cap occupancy. It does not prove windup or establish that the integral cap causes the force error.

## R32 fixed-input outer-loop shadow replay

The R32 replay was seeded from the first formal row's recorded integral and prior outer-loop translation state. With the existing controller input, the replay reproduced the recorded XYZ task request across **27,484 subsequent samples**, with maximum absolute error **0 m/s** at recorded precision. It then changed one thing in the replay: the outer loop received the scored low-pass normal signal instead of `max(filtered, measured)`. It kept the recorded pose, velocity, baseline-corrected wrench, path time, dt, gains, integral limit and task bounds fixed.

Within the formal `[5,60)` window, the filtered-only counterfactual changed the base-Z outer command by a mean **−1.631 mm/s**, median absolute **1.158 mm/s**, p95 **5.200 mm/s**, and maximum **6.000 mm/s**. The two versions had opposite base-Z command signs on **29.77%** of samples. The current normal task output is bounded at **3 mm/s**, which makes a 6 mm/s pairwise delta possible when the commands land at opposite limits.

This shadow replay is open loop. It does not feed the alternative command back into later force, pose or velocity samples. It produces no counterfactual robot motion, contact force, MAE, or safety qualification. It shows that replacing the one-sided envelope with the scoring filter is a substantial controller change, not a harmless relabeling.

## Current decision

Keep the live A profile and all current force and velocity protections fixed. The next offline branch is to separate three mechanisms using the sealed traces: the one-sided measurement envelope, task-space velocity limiting, and RNN final `Jqdot` realization. Use the R32 replay as a fixed-input counterfactual only. Do not promote the filtered-only input, a higher normal-speed limit, or a new QP profile to live without a separately reviewed safety and numerical binding. R31/R32 QP live attempts remain failed at the frozen `actual_qd` gate. TASE+SFC/DSFC remains offline only.

All force values are Kunwei feedback, not independent task-force truth. Attempt-level source hashes, definitions and sample calculations are in `tase_r31_r32_force_channel_alignment_20260926.json`.
