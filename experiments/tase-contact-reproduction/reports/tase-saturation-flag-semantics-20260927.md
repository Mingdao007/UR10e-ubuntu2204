# TASE velocity and integral flag semantics — 2026-09-27

## Finding

The historical trace field `integral_explicit_velocity_limit_active` is badly named. It copies the runtime's broad `explicit_velocity_limit_active` flag; it does **not** mean that the integral state or I-term saturated.

In `V4CalibratedRuntime`, that broad flag is set when the outer integral normal-velocity calculation clips **or** when any task-space cap changes the raw twist (normal, tangential, total linear, or angular). The provider then serializes it under the misleading `integral_...` name. The specific published field `published_task_space_saturated` is narrower: it requires a difference between requested and capped velocity along the force-normal axis. `published_realization_saturated` separately requires a normal-axis difference between the task-space command and final realized `Jqdot`, with the explicit realization-limit flag set.

## A-baseline evidence

Across the five eligible R013/rate460 parameter-confirmation A attempts, the broad `integral_explicit_velocity_limit_active` count was 29,557–30,003 ticks (roughly 99–100% of recorded ticks), while `integral_velocity_saturated`, `integral_state_clamped`, `integral_authority_clamped`, and `integral_final_output_frozen` were all recorded as zero. Those counts are not evidence that the integrator hit its limit on every cycle.

The narrower `published_task_space_saturated` count was 5,209–7,726 ticks, about **20.9%** on average. This means the requested normal velocity differed from its task-space-limited value on those ticks; it does not mean the robot stopped. `published_realization_saturated` occurred 36 times in 149,547 feedback rows (~0.024%), and host slew limiting occurred 36 times. These are distinct stages and should not be combined into one “saturation rate.”

The same five traces show the integral state within 2% of its 1.0 N·s cap on about **8.1%** of ticks on average. Their legacy recorder wrote the already-clamped state into `integral_raw_state_n_s` and fixed several clamp flags false, so those old records cannot reveal the count or magnitude of attempted raw overflow. A later diagnostic correction records the pre-clamp state; it does not change the control output.

Sources: [R013 confirmation reducer](tase-resident-confirmation-r013-diagnostic-20260927.json), [metrics/reducer report](tase-force-metric-alignment-20260927.md), `tools/step5d_autotune_v4_r004/calibrated_runtime.py`, and `tools/tase_contact_provider.py`.

## Interpretation

The 20.9% normal task-space saturation is a real observed limiter event and merits further error-correlated analysis. The near-100% broad flag cannot be attributed to integral windup. Existing data do not show that this limiter alone causes the force MAE, and do not justify loosening its cap. Keep each limiter stage separate in future reports and evaluate anti-windup using the specifically recorded integral flags plus successful-publish `Jqdot` feedback.
