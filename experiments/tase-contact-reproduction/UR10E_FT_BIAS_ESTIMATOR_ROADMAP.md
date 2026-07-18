# UR10e External F/T Bias Estimator Roadmap

This roadmap stages bias bookkeeping for the current UR10e external F/T paths.
It starts from the checked-in logging contract and deliberately separates
sensor bias, gravity/inertial model error, timing error, structural compliance,
and real contact. It defines future implementation gates; it does not enable an
online estimator, bridge, contact, or motion.

Use `UR10E_FT_CALIBRATION_REFERENCE_CHECKLIST.md` for the upstream sensor,
frame, compensation, and calibration lineage. No estimator may compensate for
an unresolved source/frame mismatch.

## Current Baseline

`tools/kunwei_rtde_bridge.py` currently provides bookkeeping, not an online
Kalman bias estimator:

- `zero_event_id` identifies completed software-baseline epochs.
- `bias_est_fx_n..bias_est_mz_nm` log the current six-axis software baseline.
- `bias_rate_est_*_per_s` is a finite difference between completed baseline
  epochs, not a continuously updated bias-rate state.
- `bias_estimation_contact_mask` and `bias_contact_reason` identify rows that
  must not update a future estimator.
- `control_contact_window` is logged separately and also forces the estimator
  contact mask.
- derived `ur_actual_qdd_*`, `ur_actual_TCP_accel_*`, and
  `ur_kinematics_dt_s` fields are available for offline model checks.

The current raw/raw-plus-fixed-offset safety guards remain authoritative. No
stage below may route corrected or filtered values into a weaker hard guard.

## Non-Negotiable Update Gate

Define one update predicate used by every estimator:

```text
update_allowed =
    baseline_ready
    and source_identity_unchanged
    and frame_calibration_hash_matches
    and sample_fresh_and_finite
    and stationary_or_declared_no_contact_excitation
    and bias_estimation_contact_mask == 0
    and post_contact_release_dwell_complete
```

When `update_allowed` is false:

- the measurement update is skipped exactly;
- bias state must not move because of the rejected sample;
- prediction-only covariance behavior is explicit and logged;
- `update_applied=0` and a deterministic `freeze_reason` are emitted;
- raw safety detection and contact classification still run.

Robust loss, low-pass filtering, or small gain is never a substitute for this
hard gate. Sustained contact is not slow bias.

## Common Estimator Record

Every future stage must emit the same reviewable fields before it can affect a
controller input:

| Group | Required fields |
| --- | --- |
| Identity | estimator version/hash, sensor source, calibration/frame hash, zero epoch |
| Timing | monotonic timestamp, `dt`, sample sequence, age/stale flag |
| Gate | contact mask, control window, stationary flag, update predicate, freeze reason |
| State | six-axis bias, six-axis bias rate when modeled, covariance diagonal and selected cross terms |
| Innovation | residual/innovation, innovation covariance, NIS, update magnitude |
| Output | raw wrench, modeled no-contact wrench, corrected wrench, update applied, confidence/valid bit |
| Provenance | input-run hash, configuration hash, software commit, claim tier |

The corrected wrench is always reconstructible from the raw row and logged
state. Estimator logs never overwrite raw data.

## Stage 0 — Logging And Replay Contract

Goal: prove bookkeeping and failure behavior before adding adaptive state.

1. Freeze the existing field names and source semantics.
2. Build deterministic synthetic fixtures for constant bias, bias ramp, zero
   events, injected contact steps, stale/nonfinite samples, irregular `dt`, and
   source/frame changes.
3. Build real-log train/tune/hold-out partitions by complete run or episode;
   never split adjacent frames across partitions.
4. Verify that contact and control masks are distinct in logs but both freeze a
   future estimator.
5. Record baseline no-contact residual, drift, contact-amplitude retention, and
   false-contact metrics without an adaptive estimator.

Promotion gate:

- every zero epoch is monotonic and hash-bound;
- mask/reason fields are complete and deterministic;
- rejected samples produce exactly zero measurement-update delta;
- raw guards and active controller commands are bit-for-bit unchanged;
- the fixed-baseline score is frozen as the comparison baseline.

## Stage 1 — Gated EMA First

Default first implementation: a six-axis gated exponential moving average
(EMA) of the no-contact residual.

Let `r_k` be the raw external-sensor wrench minus the declared no-contact
gravity/inertial/reference model at the same frame and origin. For an allowed
update:

```text
alpha_k = 1 - exp(-dt_k / tau_bias)
b_k     = (1 - alpha_k) b_(k-1) + alpha_k r_k
```

For a rejected update, `b_k = b_(k-1)` exactly. Use elapsed-time `alpha_k`, not
a sample-count constant, so rate jitter does not silently change the time
constant. Tune force and torque axes separately only with held-out evidence.

Required behavior:

- initialize from a completed software-zero epoch and log that prior;
- freeze during contact, motion outside the declared calibration excitation,
  stale data, frame/source changes, and release dwell;
- cap a single accepted update by a predeclared rate bound;
- calculate any displayed bias rate only from accepted state transitions;
- keep structural compliance, cable load, and dynamic-model residuals visible
  rather than allowing a faster `tau_bias` to hide them.

Promotion gate:

- held-out no-contact residual/drift improves over the frozen baseline;
- injected and real contact amplitude is not attenuated beyond a tolerance
  fixed before evaluation;
- state update is exactly frozen for every masked row;
- update-rate, saturation, and release-dwell diagnostics have no unexplained
  events;
- controller command and raw hard-guard decisions remain unchanged in shadow.

### Optional Gated RLS Branch

Recursive least squares (RLS) is not the default replacement for EMA. Enable a
shadow-only RLS branch only when a physical regressor is identified and
observable, such as temperature, orientation-dependent cable loading, or a
declared gravity-model residual.

- Bind the regressor definition, normalization, forgetting factor, covariance
  prior, excitation/condition-number gate, and parameter bounds.
- Freeze all RLS updates with the common contact gate.
- Reject regressors that are merely correlated with contact or path progress.
- Promote RLS only if it beats EMA on held-out runs across posture/temperature
  strata without worse contact retention or covariance collapse.

## Stage 2 — Bias And Bias-Rate Kalman Filter

Use a 12-state constant-rate model only after Stage 1 establishes the gate and
data contract:

```text
x_k = [b_k, bdot_k]
F_k = [[I, dt_k I],
       [0,      I]]
z_k = r_k
H   = [I, 0]
```

The measurement update runs only when `update_allowed`. Contact rows are
prediction-only and cannot enter the innovation. Log predicted and posterior
state/covariance separately so a covariance reset or inflation is auditable.

### Q/R Tuning Protocol

1. Estimate baseline measurement covariance `R` from actual pipeline data, not
   only vendor range/specification. Use stationary no-contact windows for each
   sensor/interface/compensation route and retain cross-axis terms when they
   are repeatable.
2. Bind the exact STARS paper version/hash before implementing its Eq. (9) or
   Eq. (12). Record which discrete propagation is used; an equation number
   without source/version binding is not reproducible evidence.
3. Propagate candidate process noise `Q` through the discrete state model and
   compare the resulting position/velocity/acceleration or bias/bias-rate
   covariance blocks against the measured `R` scale.
4. Sweep each declared base `Q` on a log scale with multipliers
   `{0.1, 1, 10}` while keeping data partitions and all other settings frozen.
5. Report innovations, autocorrelation/whiteness, normalized innovation squared
   (NIS), confidence coverage, covariance eigenvalues, and held-out residuals.
6. Introduce time-varying `R_i` only when residual variance is reproducibly
   heteroscedastic across a named condition. Introduce adaptive `Q` only when a
   fixed-Q model shows reproducible nonstationarity; neither is a first-line
   tuning knob.

### STARS-Style Joint-State Subtrack

Keep joint-state filtering separate from the F/T bias state. A per-joint
`[q, qdot, qddot]` model may supply a less noisy acceleration estimate and
covariance to the no-contact inertial-wrench model.

- Estimate encoder measurement `R_q` from real stationary encoder data and F/T
  `R_w` from synchronized stationary sensor data.
- Preserve the propagated off-diagonal covariance. It is the mechanism by which
  a position residual can update correlated velocity/acceleration states; do
  not zero those terms for convenience without a comparison artifact.
- Compare propagated position, velocity, and acceleration covariance with the
  corresponding measurement/model uncertainty before selecting `Q`.
- The joint-state filter may improve the predicted no-contact wrench, but it
  does not relax the contact mask or directly relabel contact as bias.

Promotion gate:

- Stage 2 improves held-out likelihood/NIS consistency or residual quality over
  Stage 1 without worse contact retention;
- covariance remains finite, positive semidefinite, and neither collapses nor
  grows without a logged reason;
- innovation outliers during contact are excluded by the hard mask, not fitted;
- prediction-only gaps and zero-epoch resets behave deterministically;
- shadow execution leaves active commands and raw safety decisions unchanged.

## Stage 3 — Offline Sliding Window Or Factor Graph

Evaluate this stage offline only after Stage 2 demonstrates a residual pattern
that a longer context can address.

Candidate variables and factors:

- bias/bias-rate knots and zero-event priors;
- no-contact measurement factors with exact source/frame lineage;
- joint motion and gravity/inertial-model factors with covariance;
- temperature/cable/compliance factors only after independent observability;
- discontinuity/change-point factors across remount, source switch, or zero
  epoch.

Hard boundaries:

- contact rows contribute no ordinary bias measurement factor;
- a robust loss cannot replace the contact exclusion factor;
- window smoothing may revise an offline history but may not rewrite immutable
  raw logs or zero-event provenance;
- joint motion/contact/bias estimation is permitted only when an observability
  report shows the variables can be separated;
- no real-time or controller-active path is considered until runtime budget,
  causal fixed-lag behavior, failure fallback, and shadow invariance pass.

Promote this stage only if it beats the Stage 2 hold-out result across multiple
runs and its improvement survives ablation of each added factor. Otherwise keep
the simpler Kalman stage.

## Cross-Stage Test Matrix

| Scenario | Required invariant |
| --- | --- |
| Constant no-contact offset | Converges within the declared rate/time bound without guard changes. |
| Slow bias ramp | Tracks only inside the declared update gate; rate estimate remains bounded. |
| Injected contact step/hold | Measurement update is exactly frozen and corrected contact amplitude is retained. |
| Contact chatter/release | Release dwell prevents rapid freeze/unfreeze leakage. |
| Zero event | State transition is explicit, epoch increments once, and prior/reset policy is reproducible. |
| Stale/nonfinite/out-of-order sample | No measurement update; invalid reason is logged. |
| Source/frame/calibration change | Estimator invalidates or starts a new lineage; no state is silently reused. |
| Irregular `dt` | Time-based propagation is stable and matches a resampled reference within tolerance. |
| Motion/model mismatch | Residual remains classified as model/compliance evidence, not automatically bias. |
| Raw guard threshold | Guard decision is identical with estimator shadow off/on. |

Use run-level hold-outs and report both aggregate and worst-stratum results.
Choose numerical tolerances before opening the hold-out set.

## Implementation Order And Stop Conditions

1. Complete Stage 0 fixtures and replay scorer.
2. Implement gated EMA in shadow-only mode.
3. Compare optional RLS only if a validated regressor exists.
4. Implement the 12-state Kalman filter and joint-state subtrack offline.
5. Evaluate sliding-window/factor-graph methods only if Stage 2 leaves a
   decision-relevant, identifiable gap.

Stop promotion on any contact absorption, state reuse across an invalid lineage,
nonfinite/indefinite covariance, raw-guard change, command change in shadow,
unbound source/frame/equation provenance, hold-out regression, or unresolved
compliance/model error. Completion of this roadmap never authorizes sensor
zero/tare, bridge start, Play, contact, torque, or motion.
