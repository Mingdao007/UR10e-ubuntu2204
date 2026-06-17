# Step6 8-Shaped Flow

Step6 promotes the paper Experiment #2 8-shaped path into the current Kunwei
flow selected by `config/current_stage.json`. Step4g remains historical
evidence only.

## Selected Target

| program | owner | contact | bridge | target |
|---|---|---:|---:|---|
| `step6b_contact_eight_baseline_v2` | TP + bridge | true | true | `/programs/andyl/kunwei/step6/step6b_contact_eight_baseline_v2.urp` |

Step6a remains retained no-contact rehearsal evidence. Step6b v1 remains
retained contact evidence: it completed the runtime/state-machine contract, but
its 4 mm/s path cap and 6 mm/s total cap were infeasible for the Step6 8-shaped
reference.

For Step6a and Step6b ROS2 migration, historical replay, and live-gated refactor
work, the authoritative Local Control textbook is
`config/local_control_textbook_spec.json` plus `config/step6_stage_table.json`,
`config/step6_eight_safe_frame.json`,
`programs/step6/step6a_eight_no_contact_v1.script`,
`programs/step6/step6b_contact_eight_baseline_v2.script`, retained Step6b v1
provenance, and relevant Auditor reports under `/home/andy/codex_handoffs/`.
Implementor handoffs and completion reports must first list these textbook
sources under `Textbook Sources Inspected`, then include a
`Local Control Textbook Alignment` table that marks each field as preserved,
changed with reason, or out of scope. Step6a no-contact fixed-Z/cadence/caps and
Step6b contact scaffold, 5 N target, filtered-live normal correction, bridge
limits, feasibility correction, guards, and operator-only recovery surfaces must
be explicit classifications. This textbook gate is offline evidence only; it does
not authorize bridge start, TP Play, upload, live motion, contact, payload/TCP
writes, safety writes, or `zero_ftsensor()`.

## Waypoint Calibration

Step6 starts with five manually positioned, read-only RTDE snapshots:

| label | phase rad | t s | local along mm | local lateral mm |
|---|---:|---:|---:|---:|
| `center_start` | 0 | 0.000 | 0.000 | 0.000 |
| `right_upper` | pi/4 | 3.927 | 28.284 | 10.000 |
| `right_lower` | 3pi/4 | 11.781 | 28.284 | -10.000 |
| `left_upper` | 5pi/4 | 19.635 | -28.284 | 10.000 |
| `left_lower` | 7pi/4 | 27.489 | -28.284 | -10.000 |

Formula:

```text
along = 0.04 * sin(0.2t)
lateral = 0.01 * sin(0.4t)
duration = 30 s
```

## Contact Baseline

Step6b uses the successful Step5b contact scaffold:

- first-contact normal latch
- 20 mm lift
- 25.2 attitude correction
- second contact
- 25.3 line-entry gate
- 25.0 bridge-owned active 8-shaped path for 30 s

Bridge contract:

```text
--step4e-version step6b_v2 --step4e-path-shape eight --target-force-n 5.0
```

The active path is the Step6 five-waypoint safe frame, not the old Step4g line
midpoint frame:

```text
along = 0.04 * sin(0.2t)
lateral = 0.01 * sin(0.4t)
duration = 30 s
```

Bridge speed profile:

```text
step4e_motion_limit_m_s = 0.015
step4e_total_linear_limit_m_s = 0.015
step4e_normal_velocity_limit_m_s = 0.003
step4e_angular_limit_rad_s = 0.060
```

Offline feasibility gate:

- Step6 reference max speed is 8.944 mm/s.
- With a 3.0 mm/s normal reserve, required total linear speed is 9.434 mm/s.
- The v2 15 mm/s path and total caps pass; v1's 4/6 mm/s caps fail.
- 25.2 attitude capacity is 0.060 rad/s * 8 s = 0.480 rad, covering the
  0.523599 - 0.052360 = 0.471239 rad design window.

Timing:

- `speedl(..., 0.001)` is kept as the near-500Hz cadence request.
- Path time is advanced with `get_steptime()`, not the requested hold value, so
  the 30 s active stage tracks controller elapsed time instead of stretching to
  about 60 s on a 2 ms execution cadence.

The safe frame is no-scale rigid rotation + translation from those five points.
If residuals or the X guard fail, no TP package is generated or uploaded.

## Boundaries

- Step6a: no contact and no Kunwei bridge.
- Step6b: contact motion through the bridge only after explicit operator
  confirmation.
- No `zero_ftsensor()`.
- No TCP/payload write.
- Controller delivery is file upload + read-back verify only; it is not load,
  Play, URScript send, or motion.
