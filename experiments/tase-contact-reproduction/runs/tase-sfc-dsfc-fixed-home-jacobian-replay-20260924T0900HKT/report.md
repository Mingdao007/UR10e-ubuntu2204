# TASE + SFC/DSFC calibrated fixed-Home Jacobian command sensitivity

Evidence level: **offline command substitution on model-generated traces**. This is not a new closed-loop plant simulation, not a UR10e trial, and not an independent force measurement.

Source: `runs/tase-sfc-dsfc-offline-20260924T0558HKT`; 15 complete 60 s traces were each replayed once with SFC and once with DSFC (30 controller replays, 30000 ticks per replay). The same transformed input digest matched within every pair.

The only deliberate input substitutions were the canonical Figure-eight Home joint position, FK rotation/position rebasing, and the calibrated TCP Jacobian evaluated at that fixed Home. The calibration model predicts Home position within 0.024 mm of the approved TCP pose; Jacobian condition number is 8.446. The 5 N target, A normal controller, force traces, estimated normals, references, timestamps, and ±0.05 rad/s per-joint bounds remain from the existing comparison protocol.

| Model-trace scenario | SFC tangential-command RMS (m/s) | DSFC tangential-command RMS (m/s) | At least one joint at ±0.05 (SFC / DSFC) | Bound violations (SFC / DSFC) |
|---|---:|---:|---:|---:|
| normal_pulse | 0.00198468968 | 0.00207922519 | 99.98667% / 99.98667% | 0 / 0 |
| plane | 0.00178563361 | 0.00178327946 | 99.98667% / 99.98667% | 0 / 0 |
| tangent_pulse | 0.00203267916 | 0.0021505062 | 99.98667% / 99.98667% | 0 / 0 |

Every replay had 29,996 of 30,000 samples at a joint-velocity bound (99.987%); no output exceeded the bound. Each tick still made exactly one final TASE realization, and maximum tangential leakage into the estimated normal direction was 8.56e-19 m/s.

This is a feasibility warning, not an SFC-vs-DSFC performance win: commands derived from the identity-J closed-loop model nearly always reach the velocity cap when their recorded inputs are substituted onto the calibrated Home Jacobian. The replay keeps each model trace fixed, so later forces and poses do not respond to the substituted commands. Do not treat its output as a physical saturation rate or a normal-force MAE.

The raw receipt also stores an unweighted 6-vector `Jqdot - desired_twist` norm. That vector combines m/s and rad/s, so this report does not interpret it as a dimensionally homogeneous error measure.

No live controller, writer, bridge, sensor, apparatus, human push, or force zeroing was used. The fixed-Home Jacobian substitution is not authorized for live control by this result.

Reproduction: `tools/tase_sfc_dsfc_replay.py --comparison-dir runs/tase-sfc-dsfc-offline-20260924T0558HKT --fixed-home-ur10e-jacobian --output runs/tase-sfc-dsfc-fixed-home-jacobian-replay-20260924T0900HKT/command-replay.json`.
