# R32 A force-to-motion chain diagnostic

**Evidence class:** sealed complete live A trace, joined offline diagnostic only. The controller, task limits, and live profile were not changed.

Input: `/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/runs/tase-r32-rate460-a-v3-capture-evidence-only-20260926T171700HKT/attempts/0001`, protocol `r013_60_rate460`, formal window `[5,60)`, 27485 rows. Timeline SHA-256: `c296074b4a583709a9fe810d6680bd7a39f515849c932b4a2ba6c8967c8975ee`.

| Stage | Observed on this trace | What it means |
|---|---|---|
| Force input | `max(filtered, measured)` is 0.882 N above the scored filter on average; controller-channel mean error is 0.198 N while score-channel mean error is 1.080 N. | This is an exact same-sensor signal mismatch; it is not independent force truth and does not alone prove a physical MAE cause. |
| Task-space normal limit | Active on 22.60% of formal rows. On clipped rows, score-channel sample MAE is 2.347 N; unclipped rows are 0.736 N. | The clipped subset is selected by higher error and requested speed, so this contrast is associative, not causal. |
| RNN joint-velocity realization | `task request − final Jqdot` absolute p95 is 2.802 mm/s; even the no-explicit-bound/slew subset has p95 2.810 mm/s. | This is a measured calibrated-Jacobian command residual. It is distinct from joint-velocity correlation and suggests the RNN output does not fully realize every normal task request. |
| Robot speed readback | RTDE `actual_TCP_speed` and `J·actual_qd` normal projections differ by at most 0.000025 mm/s in the saved values. Command/readback correlation peaks at lag 5 samples (8.79 ms median publish-to-sample offset; correlation 0.717). | Internal command/readback alignment only; RTDE speed is state-derived and the peak lag is not a causal plant-delay estimate. |

The exact command decomposition is `requested → task-capped → final Jqdot`. It shows where command magnitude changes, but because the trace is a closed-loop outcome, it cannot allocate a causal share of force MAE to the force envelope, task cap, or RNN realization. The reported force signals all come from the Kunwei feedback chain.

## Claim boundary

- Fixed-input/observational analysis only; no candidate commands were fed back into the robot.
- The control-force and scored-force channels are not independent measurements.
- This does not establish that removing the envelope, increasing the task-speed cap, or changing the solver improves contact force.
- Live TASE-RNN A remains unchanged. TASE-QP and SFC+TASE remain at their previously recorded evidence levels; no new live run is admitted by this report.
