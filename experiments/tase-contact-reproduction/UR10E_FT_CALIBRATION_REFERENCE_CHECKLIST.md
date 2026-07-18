# UR10e External F/T Calibration And Reference-Chain Checklist

This checklist governs calibration evidence for the current UR10e external
force/torque paths. It covers the historical OnRobot HEX-E v2 path and the
current Kunwei KWR75B path without treating their outputs as interchangeable.
It is an offline procedure and evidence contract, not proof that calibration,
contact readiness, or live motion has passed.

## Claim Boundary

- Completing this document means the procedure is defined. A calibration is
  accepted only after every applicable gate below has hash-bound run evidence.
- Any pose change, dynamic sweep, applied reference load, sensor zero/tare,
  payload/TCP write, bridge start, program Play, or contact trial remains behind
  its UR10e live owner and explicit authorization.
- Package read-back, version read-back, or a quiet force trace cannot substitute
  for a complete sensor-to-contact wrench reference chain.

## Keep The Sensor Paths Separate

Record the exact source in every artifact. Never label all of these simply
`F/T sensor`:

| Source | Current interpretation | Mandatory caveat |
| --- | --- | --- |
| OnRobot URCap `Fx..Tz` | URCap/PolyScope variable layer; can be exported through RTDE registers | Its zero/reference/compensation path is not proven equal to Compute Box TCP/UDP. |
| OnRobot Compute Box TCP/UDP | Direct Compute Box data path | Existing evidence shows a large reference offset relative to URCap variables; do not mix baselines. |
| UR `actual_TCP_force` | UR controller estimate at the configured TCP | It is not an external-sensor ground truth. |
| Kunwei TCP stream | Factory-decoded sensor wrench converted from `Kg`/`Kg*m` to SI | Current bridge zero is a software baseline, not a sensor tare. |

For each run, bind `sensor_family`, device identity when known, interface,
firmware/URCap version, sample rate, units, axis order, factory calibration
identity, zero policy, and compensation mode.

## Canonical Reference Chain

Use four named frames:

- `S`: sensor measurement frame at the sensor origin.
- `T`: configured TCP frame at the TCP origin.
- `B`: UR base frame; when a wrench is expressed in `B` for control, retain the
  TCP origin unless the artifact explicitly shifts the moment reference.
- `C`: contact frame with a declared `reaction_normal`.

For a geometric transform from `S` to `T`, let `R_TS` rotate vectors from `S`
into `T`, and let `p_TS_T` be the vector from the TCP origin to the sensor
origin, expressed in `T`:

```text
f_T   = R_TS f_S
tau_T = R_TS tau_S + p_TS_T × f_T
```

Rotating that same-origin wrench into base coordinates is:

```text
f_B   = R_BT f_T
tau_B = R_BT tau_T
```

If the moment is shifted to another origin, add the corresponding
`p_origin_to_application × f` term and name both origins. A rotation-only
conversion is invalid when sensor and TCP origins differ.

The processing chain must be explicit and must not double-compensate:

```text
counts/vendor tuple
  -> factory calibration and unit conversion
  -> electrical/software bias policy
  -> payload gravity/inertial compensation, if this source does not already do it
  -> wrench transform with moment shift
  -> base/contact expression at a named origin
  -> raw safety guard and controller input
```

For contact semantics, follow `UR_FORCE_FRAME_CONTRACT.md`:

```text
approach_normal = -reaction_normal
normal_load_n   = dot(force_base, reaction_normal)
force_error_n   = target_load_n - normal_load_n
```

Raw live force must not be normalized into a posture target.

## Evidence Bundle Before Any Fit

- [ ] Robot serial, PolyScope version, Safety state, program state, and capture
      timestamp are recorded through read-only evidence.
- [ ] Sensor identity, interface, firmware/URCap version, factory calibration
      identifier/checksum, units, axis order, and nominal range are recorded.
- [ ] Mechanical stack order from robot flange to contact point is drawn and
      each adapter, quick changer, fastener set, cable load, and tool is named.
- [ ] Nominal `T_S`, TCP, payload mass, payload CoG, and their provenance are
      recorded without writing them to the robot during an offline audit.
- [ ] Sampling clocks, sequence counters, host timestamps, expected rate, and
      dropped/stale-frame policy are defined.
- [ ] The raw stream is preserved. Corrected data never replaces raw evidence.
- [ ] Contact masks, control-contact windows, and zero-event epochs are separate
      fields.

## Gate 1 — Stationary Baseline And Zero Epoch

1. Use a confirmed no-contact, stationary window with constant mounting,
   payload, cable routing, temperature band, and sensor source.
2. Record raw six-axis mean, covariance, robust median/MAD, drift slope, and
   outliers before applying a new software baseline.
3. Create a monotonically increasing `zero_event_id`; bind its input window,
   estimator, output vector, timestamp, and source hashes.
4. Preserve sensor tare/UR `zero_ftsensor()` as prohibited unless separately
   authorized. The default Kunwei route changes only a host-side offset.
5. Fail if contact is present, the robot is moving, samples are stale/nonfinite,
   the source identity changes, or the baseline window is not reproducible.

Acceptance requires the measured baseline uncertainty and drift to fit inside a
declared error budget that is strictly smaller than the lowest intended contact
threshold. The artifact must state the numerical budget; this checklist does
not invent one from vendor range alone.

## Gate 2 — Static Gravity And CoG Identification

After separate live authorization, collect at least six well-separated tool
orientations plus repeated hold-outs. Keep the tool and cable stack unchanged.

- Fit electrical bias, payload mass, and payload CoG only from no-contact static
  windows with a declared measurement model.
- Use held-out orientations to test the fitted gravity wrench; do not report
  training residual as validation.
- Report per-axis bias, covariance, force/torque residual RMS and p95, condition
  number, parameter confidence, and cross-axis correlation.
- Reject negative/nonphysical mass, poorly observable CoG, orientation-dependent
  residual structure, or a fit whose confidence interval overlaps the contact
  budget.
- Keep OnRobot URCap-compensated and direct Compute Box fits separate. A fit for
  one source cannot calibrate the other.

## Gate 3 — No-Contact Dynamic Sweep

After separate no-contact motion authorization, log synchronized sensor data,
`q`, `qd`, derived `qdd`, TCP pose/speed/acceleration, payload/TCP read-back,
temperature when available, and zero epoch.

- Cover the pose, speed, acceleration, and cable-deflection envelope intended
  for the later experiment without contact.
- Evaluate residuals against pose, velocity, acceleration, direction reversal,
  temperature, and time since zero.
- Separate delay/clock error, inertial-wrench error, cable force, mount
  compliance, and electrical drift; do not collapse all residuals into bias.
- Fail if a static calibration appears quiet only because dynamic residuals are
  phase-shifted, pose-dependent, or large relative to the contact budget.

## Gate 4 — External Force And Moment Reference

Use a calibrated reference device or traceable masses at declared application
points. The setup must cover both force sign and lever-arm moment:

- Apply positive and negative loads along each feasible sensor/TCP axis.
- Apply loads at two or more known lever arms so the `p × f` moment shift is
  independently observable.
- Compare raw, bias-corrected, gravity-compensated, TCP-frame, and base-frame
  results against the same reference event.
- Report scale error, offset, hysteresis, cross-axis coupling, repeatability,
  sign closure, and moment-reference closure with declared tolerances.
- Fail on a sign inversion, wrong moment origin, undocumented axis permutation,
  or a tolerance selected after seeing the validation result.

## Gate 5 — Non-Rigid EOAT And Local Stiffness

The current end stack is not assumed to be one continuous rigid body. The
nominal rigid transform remains necessary, but it may be insufficient under
load.

- Record deflection at multiple robot postures, normal/tangential load levels,
  load directions, and excitation-frequency bands.
- Estimate a local compliance/stiffness map only inside observed bins, for
  example `delta_x ≈ C_local w` and `K_e,local = C_local^-1` when the matrix is
  identifiable and well-conditioned.
- Keep structural deflection, backlash, cable loading, viscoelastic creep, and
  sensor bias as different hypotheses with different evidence.
- Fail the single-rigid-transform model when load-dependent displacement or
  moment error exceeds the declared TCP/contact budget or changes materially
  across posture/frequency bins.
- Do not absorb compliance into sensor bias and do not derive impedance gains
  from a local `K_e` estimate without a separate controller-stability analysis.

## Gate 6 — Contact And Bias Protection

- `bias_estimation_contact_mask` must be distinct from the control contact
  window but become active for either sensor-threshold contact or an active
  control-contact window.
- Every estimator update must be exactly frozen while the mask is active and
  through a declared post-contact release dwell.
- Raw/raw-plus-fixed-offset values retain hard force/torque guards; filtering
  only the controller input cannot weaken safety detection.
- Replayed injected contact steps must retain their amplitude after correction;
  any slow estimator that turns sustained contact into baseline fails.
- A new zero epoch during contact, stale data, motion, or an unresolved source
  switch is prohibited.

## Acceptance Record

A calibration lineage is accepted only when its manifest contains:

- raw data hashes and immutable source paths;
- sensor/interface/calibration identity and software versions;
- mechanical stack, nominal transforms, TCP/payload/CoG read-back, and moment
  origins;
- zero epochs, fitted parameters, covariance, train/hold-out split, and
  residual metrics;
- static, dynamic, external-reference, compliance, and contact-mask results;
- explicit pass/fail tolerances chosen before validation;
- known validity envelope and invalidation triggers;
- reviewer-independent deterministic validation command and result;
- claim boundary stating whether the result is offline-only, no-contact, or
  separately live/contact accepted.

Changing sensor source, URCap/firmware, mounting stack, adapter, contact point,
TCP, payload/CoG, cable routing, factory calibration, zero policy, sampling
path, or compensation mode invalidates the affected lineage and requires a new
epoch. A completed checklist never authorizes Play, bridge start, zero/tare,
contact, or motion.
