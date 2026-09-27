# TASE R013 phase-error ILC screen — 2026-09-27

## Finding

The five already-completed rate460 A traces contain some repeatable phase structure in the **filtered scoring channel**, but the actual fused normal-force input does not support the same phase-template predictor. No ILC velocity correction was generated or tested. This screen does not justify a live ILC candidate.

## Method and result

The five eligible A attempts each provide 550 formal `[5,60)` and 600 diagnostic `[0,60)` equal-weight 100 ms bins. A leave-one-attempt-out predictor was trained on four attempts and evaluated on the fifth. The phase template was compared with a single training-set global-mean error, so a constant force offset alone does not count as phase-repeatable structure.

| KWR75 channel | Window | Pooled residual MAE: global mean → phase template | Median held-out phase skill |
|---|---|---:|---:|
| Filtered scoring channel | `[5,60)` | 0.757 → 0.613 N | +26.1% |
| Filtered scoring channel | `[0,60)` | 0.752 → 0.623 N | +22.0% |
| Actual controller input `max(filtered, measured)` | `[5,60)` | 0.987 → 1.059 N | −27.2% |
| Actual controller input `max(filtered, measured)` | `[0,60)` | 1.002 → 1.085 N | −30.0% |
| Measured KWR75 channel | `[5,60)` | 1.813 → 1.834 N | −27.0% |

Positive filtered-channel prediction skill is descriptive: it indicates a repeatable component in the smoothed score signal. The controller receives the fused channel, where the same phase template predicts worse than a global offset. An update learned only from the filtered metric would therefore be disconnected from the live controller input.

## Decision and limits

Do not start ILC from this template. No velocity update, gain, or update rule was fitted. These are held-out feedback-prediction errors, not candidate force MAEs or robot counterfactuals. KWR75 remains the feedback signal, not independent task-force truth. No live attempt or sensor/robot write occurred.

The machine-readable result preserves all five source timeline digests and per-fold scores at `runs/tase-r013-A-normal-error-phase-template-cv-20260927.json` and `runs/tase-r013-A-control-input-phase-template-cv-20260927.json`.
