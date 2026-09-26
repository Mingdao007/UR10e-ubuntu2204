# R32 TASE-RNN A baseline and sealed-input controller replays — 2026-09-26

## Scope

This record covers one rate460 A Figure-eight unit and offline QP/SFC/DSFC command replays on its sealed TASE-RNN trace. It does not combine RPSFC/SFC/DSFC drift measurements with contact-force results. Kunwei remains the force-feedback source, not an independent task-force truth sensor.

## Live A unit

- Method `TASE_RNN_MATURE`; protocol `figure8_window60_r013_rate460_v1`; target 5 N; 60.0 s PATH; formal score window `[5,60)`; 550 bins.
- Parameters were the approved A profile: `Md=9.565272137974492`, `Bd=693.6559295653944`, legacy integral limit `1.0 N·s`.
- The full PATH completed, the evidence and timing gates passed, and automatic joint Home was verified. The formal binned normal-force MAE was **1.098445 N**. RTDE actual-qdot correlation was **0.968193**; maximum XY path error was **0.3925 mm**.
- Timing layers passed the frozen 460 Hz minimum: host publishes **499.683 Hz**, RTDE frames **500.000 Hz**, distinct TP-consumed echoes **480.936 Hz**, and Kunwei samples **1000.100 Hz**. The complete drained echo set contained 30,004 RTDE frames and 28,860 distinct consumed PATH sequences. Do not treat host publish count as the TP-consumed count.
- The lifecycle span from first consumed PATH echo through the TP `RETURNING` proof was **62.424 s**, which includes the ending handshake. Formal reference coverage was **59.998 s + 0.002 s = 60.000 s**. Keep those protocol durations separate.
- Stage 20 lasted **9.304 s**, stage 21 **8.868 s**, and state 25 covered the PATH/ending handshake. Through-seal time was **85.510 s** for this cold unit; it does not estimate the ordinary reused-cycle median.
- Per-command filtered-force diagnostics were MAE **1.1265 N** over `[0,60)` and **1.0999 N** over `[5,60)`, with `[5,60)` RMSE **1.4620 N** and bias **−1.0801 N**. These sample-level diagnostics are not the official 550-bin metric and are not independent force truth.

The first preparation attempt used `video-policy=required` and stopped before dispatch because the recorder stopped growing after 1.933 s; it made no motion. The second attempt used the already-approved `evidence-only` policy, recorded video with no recorder error, and retained continuous RTDE/Kunwei evidence. The final dispatch receipt records the actual Dashboard STOP, stationary joint Home, Safety NORMAL, and no cleanup errors. The per-attempt `resident_playing` field described readiness inside its session; the session-level stop receipt and the subsequent read-only preflight show the TP stopped. A follow-up `--stop` now acknowledges a verified prior stop when its owner process has exited; it explicitly labels that as prior evidence rather than a fresh state read.

Sealed unit: `runs/tase-r32-rate460-a-v3-capture-evidence-only-20260926T171700HKT/attempts/0001/`.

## Replay input audit

The new v3 command timeline is seal-verified and complete: **29,989/29,989** captured host PATH command inputs, no missing captures or packet joins, with force/wrench, pose/speed, fixed control axis, reference, Jacobian, bounds, state age, integral/saturation state, and actual q/qd. The distinct TP-consumed sequence count is smaller because the controller consumes fewer commands than the host publishes; replay consumes the recorded host-tick observation sequence and remains open loop.

Observed `dt` median was **1.9999 ms**, p99 **2.2131 ms**, maximum **12.0680 ms**. The SFC/DSFC adapter now uses each measured interval; when it exceeds the native law's 4 ms single-step limit, it divides the interval into equal steps and holds that one recorded sample constant. Six rows used more than one substep. This adds no synthetic intermediate sensor values, but the held-input integration is an explicit numerical approximation.

## TASE-QP fixed-input replay

The strict equality QP solved **29,989/29,989** independent frozen RNN outer-input rows, with no rejected rows. Maximum equality residual was **1.011e−8**; median/p95/p99/max solver time was **0.0312/0.0333/0.0666/0.5794 ms**. Median infinity-norm qdot difference from the published RNN command was **0.004315 rad/s**; maximum post-slew task residual was **0.002362**.

This validates the algebraic solver on fixed inputs only. It does not feed QP motion back into the force loop, does not recompute force MAE, and does not overturn the earlier live QP failures at the measured-qdot correlation gate. Do not repeat the same live profile unchanged.

Replay: `runs/tase-r32-a-v3-qp-replay-20260926T173000HKT/`.

## TASE-RNN + SFC versus TASE-RNN + DSFC fixed-input replay

Both frozen native laws consumed the same **29,989** recorded input rows with common-input SHA-256 `9c2bf7984fb564bb89882014f0a7d23c75e69c15ef96aa8b13e53f022b93c17c`. Each tick used one final joint-velocity realization. The candidate command streams differ: the per-row infinity-norm qdot difference had median **0.003415 rad/s**, p95 **0.005817 rad/s**, and maximum **0.006861 rad/s**.

Under the current *offline-only* profile, both branches hit at least one `0.05 rad/s` joint bound on **29,825/29,989** rows (99.45%) without a bound violation; maximum `Jqdot` residual was **0.12071 m/s**, RMS **0.06284 m/s**. The frozen model's `K_path=120 N/m` is not approved for live use. The high bound-hit rate and residual mean this profile is not ready to promote to live. SFC and DSFC tangential command RMS values were **0.002510** and **0.002972 m/s**; their TASE normal/orientation terms were identical. Tangential-to-normal leakage was zero in this fixed-axis replay.

These results are an open-loop counterfactual over recorded feedback: candidate commands were not sent to the robot and were not fed back into later observations. The force signal is baseline-corrected Kunwei feedback, and the normal is the fixed base-Z TASE control axis, not independent truth or a surface-normal estimate. No candidate force-MAE improvement is computed.

Replay: `runs/tase-r32-sfc-dsfc-live-trace-replay-final-20260926T173500HKT/`.

## Decision-tree update

- The 460 Hz A branch has a new complete, eligible rate460 unit with a v3 trace. Keep the 460 Hz floor and keep A as the baseline; this is one unit, not a new randomized comparison.
- A separate R31/R32 trace audit now confirms that the controller uses `max(filtered_normal, measured_normal)` while the score uses `filtered_normal`. Across four complete A traces the controller input averaged 0.808–1.020 N above the score signal. The R32 fixed-input outer-loop shadow reproduces the baseline XYZ request exactly, then shows that filtered-only input would materially change commands. This is a confirmed signal-path difference and an offline counterfactual, not a causal improvement claim or an approved live change. Full evidence: `reports/tase_r31_r32_force_channel_alignment_20260926.md`.
- The QP branch is algebraically feasible on the same frozen RNN inputs, but still has no closed-loop force evidence and no reason for an unchanged live retry.
- The SFC/DSFC identical-input replay is now possible for a sealed physical feedback trace, but saturation under the current offline proxy prevents live promotion. Next: review the tangent/path numerical profile and bound behavior offline, then request explicit value review before any live composition.
- No measured 5 N environment/contact second-order model or SFC/DSFC live effect is established. Do not claim improvement from this replay.
