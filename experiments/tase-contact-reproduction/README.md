# Kunwei Closed-Loop Straight-Line Experiment

This package implements the 2026-06-04 Kunwei replacement for the previous
OnRobot URCap straight-line run. It rebuilds the target line from the prior
successful OnRobot evidence, but the live control path is:

1. Ubuntu Python reads Kunwei TCP frames from `192.168.50.25:5152`.
2. Python subtracts a no-motion baseline and converts manual `Kg`/`Kg*m` units
   to `N`/`Nm`.
3. Python writes UR RTDE input registers `24..36` at about `125 Hz`.
4. A reviewed URScript on the teach pendant reads those registers and runs
   controller-side `speedl()` for the line and normal-force correction.

The scripts do not write UR TCP, UR payload, UR force zero, Kunwei tare, Kunwei
filter, or Kunwei network configuration.

## Delivery Check

Before publishing, handoff, package delivery, or any live-trigger claim from
this experiment root, run:

```bash
./check.sh
```

This local gate runs the protocol-table validator, cross-step stage-table
validator, and the repository pytest suite with `-p no:anyio`. Passing it is an
offline delivery precondition only; it does not authorize bridge start, TP Play,
controller upload, or robot motion.

## Who Sends What

Use a split-control workflow:

- Ubuntu command line starts preflight, Kunwei logging, software zero, and RTDE
  input-register bridge.
- The teach pendant remains the motion authority. You choose the reviewed
  Script program and press Play there.
- Do not send motion URScript from Ubuntu for these steps. Remote Control can
  support that technically, but it removes the clear human decision point we
  want here.

Step numbering:

- `Step0`: no-motion register echo and frequency benchmark.
- `Step1`: full no-contact pipeline, the final-run scaffold without touching
  the surface.
- `Step2`: real contact search, pilot closed-loop, and final repeats.

Step0 operator command wrapper:

```bash
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step0-operator.sh preflight
```

Then use either:

```bash
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step0-operator.sh benchmark-freq
```

for the no-motion highest-frequency check.

Step1 operator command wrapper:

```bash
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step1-operator.sh bridge
```

Step2 operator command wrapper:

```bash
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step2-operator.sh bridge-search
```

Step2D circular-contact package:

```bash
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/tools/build_step2d_circle_program.py
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step2d-circle-v1-operator.sh autowatch
```

Teach Pendant target:

```text
/programs/andyl/kunwei/step2/step2d_circle_no_contact_v1.urp
/programs/andyl/kunwei/step2/step2d_circle_full_paper_attitude_v1.urp
```

Step2D reuses the Step2C v8 scaffold for high approach, contact search, soft
acquisition, short retract, and home return. The controlled-contact stage is
changed from a straight line to a full circle whose diameter is the middle half
of the prior contact path: `25% -> 75%`, radius `15.895 mm`, full arc length
`99.871 mm`. The UR still owns IK through Cartesian `speedl()` twist commands.
The first Step2D program includes bounded `wx/wy` attitude compliance based on
lateral force plus `Mx/My` as a paper-first force-shortest-arc proxy; treat that
as a v1 experimental controller, not as a validated paper-faithful claim until
there is run evidence.

Execution order is intentionally staged. First open and run
`step2d_circle_no_contact_v1.urp`; it uses the same circle geometry at safe Z
with no Kunwei stream, no RTDE input dependency, no contact search, no force
control, and no attitude compliance. Only after that dry run is visually and
logically acceptable should the operator open
`step2d_circle_full_paper_attitude_v1.urp` and use the Step2D bridge/autowatch
wrapper.

For Kunwei logging without RTDE input writes:

```bash
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step0-operator.sh bridge-minimal-log
```

The bridge commands ask for a typed confirmation before sending Kunwei stream
commands. The teach pendant scripts are deployed on the UR controller under
`/programs/andyl/kunwei/`; after `step1-operator.sh bridge` is running, run
`/programs/andyl/kunwei/step1_full_no_contact_pipeline.script` from the teach
pendant.

## Control Mode And Data Path

The intended mode is **TP-started, RTDE-assisted**:

- You start the reviewed URScript from the teach pendant.
- Ubuntu does not send motion URScript to ports `30001/30002/30003`.
- Ubuntu reads Kunwei TCP data and writes UR RTDE input registers
  `24..36` on port `30004`.
- The running URScript reads those values with `read_input_float_register()`.
- Any closed-loop `speedl()` command is computed inside the URScript running
  on the controller, not streamed from Python.

So "local control" can still be compatible with the architecture if the current
controller accepts RTDE input-register writes while a teach-pendant-started
program is running. Do not assume that silently: prove it first with the echo
test below. If echo does not work in the current mode, then the fallback is to
switch to a true remote/headless workflow or to a different explicit data path,
not to guess.

Official UR interface facts behind this split:

- In local mode, URScript commands over `30001/30002/30003` are not allowed.
- RTDE is a separate TCP/IP synchronization interface on `30004`.
- `read_input_float_register(24..47)` is the URScript-side path reserved for
  external RTDE clients.

No-motion echo test:

```bash
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step0-operator.sh bridge-echo
```

Then run this on the teach pendant:

```text
/programs/andyl/kunwei/step0_register_echo.script
```

Pass condition: the bridge log shows output registers `24..35` changing with
the same values written into input registers `24..36`. Only after that passes
should `step1_full_no_contact_pipeline.script` be trusted to read Kunwei values.

## Step0 Highest-Frequency Benchmark

Before any new no-contact line or contact run, use the no-motion frequency
benchmark:

```bash
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step0-operator.sh benchmark-freq
```

Operator split:

- Ubuntu sends the Kunwei stream command and writes RTDE input registers.
- Teach pendant runs `/programs/andyl/kunwei/step0_register_echo.script`.
- After typing `START_FREQ_BENCH`, immediately press Play on the teach pendant;
  the benchmark spends the first `3 s` on Kunwei baseline, then starts the
  frequency sweep.
- The echo script contains no `speedl`, `movel`, TCP/payload write, zero, tare,
  or Kunwei configuration command.

The benchmark requests `125, 250, 500, 1000 Hz` and writes:

- `summary.json`: per-rate send rate, RTDE output rate, echo heartbeat
  transition rate, max gap, and pass/fail.
- `rtde_echo_events_<rate>hz.csv`: raw RTDE send/output timing evidence.
- `raw_frames.bin`: raw Kunwei frames captured during the run.

Strict `1 kHz` pass means output-register echo heartbeat transitions at
`>=950 Hz` with max echo gap `<=3 ms`. If Python can send at `1 kHz` but the
UR echo transitions lower, that is not a `1 kHz` robot-side control pass.

Remote Control can be used later to compare launch/control authority, but it
does not by itself prove a higher UR controller-side rate. Judge Local and
Remote only by the measured echo rate in this no-motion benchmark.

## Frequency Budget

Do not describe the current Step1/Step2 scaffold as `1 kHz` force control.

Current motion-control implementation:

- Kunwei TCP frame parsing: sensor-side stream is expected at about `1 kHz`.
- Ubuntu bridge output to UR RTDE input registers: configured at `125 Hz`
  by `--rtde-hz 125`.
- URScript loop in the checked-in Step1/Step2 programs: `dt = 0.008`, so nominally
  `125 Hz`.
- Therefore, the active force-control input path is `125 Hz`, while the raw
  Kunwei log can still preserve `1 kHz` sensor data.

Why this is intentional for Step0 and Step1:

- The previous OnRobot URCap register export evidence was also about `125 Hz`,
  and the first goal is to prove direction, sign, guard, and data-chain
  behavior before increasing bandwidth.
- A faster RTDE setting may be possible on e-Series controllers, but it must be
  measured on the actual bench and matched by the URScript control loop. Do not
  assume `1 kHz` is available through RTDE/register-based URScript control.

If the no-motion benchmark proves a higher stable rate, the next candidates are:

- test `250 Hz` or `500 Hz` RTDE/register updates and change URScript `dt`
  accordingly, with measured echo and motion logs;
- evaluate UR external F/T RTDE injection separately;
- keep Kunwei `1 kHz` data for filtering/analysis while controlling at the
  highest measured stable UR-side rate.

## 1 kHz Control Requirement

If `1 kHz control frequency` means the robot-side motion command or force-control
correction must be updated every `1 ms`, the current UR10e RTDE/register/URScript
architecture does not satisfy the requirement.

Evidence-based boundary:

- Kunwei can provide about `1 kHz` sensor data.
- UR e-Series robot/controller update paths exposed through standard external
  interfaces are normally bounded at or below `500 Hz`, and RTDE may be lower
  depending on controller/software/interface path.
- The checked-in Step0/Step1 bridge is deliberately configured at `125 Hz`.

Therefore, do not claim that tuning `--rtde-hz`, using Local/Remote Control, or
starting the same URScript differently makes this a true `1 kHz` robot-side
force controller.

Architectures that could satisfy a strict `1 kHz` force-control requirement:

- Add an end-effector-side real-time controller, such as a small linear Z
  compliance actuator, voice-coil stage, or dedicated embedded controller, that
  reads Kunwei at `1 kHz` and closes the normal-force loop locally. UR10e then
  supplies lower-frequency XY motion and safety supervision.
- Use a robot/controller platform with a documented `1 kHz` external servo or
  force-control interface.
- If the real requirement is only `1 kHz force sensing`, keep the Kunwei `1 kHz`
  acquisition path and explicitly state that robot motion control runs at the
  measured UR-side rate.

Practical decision for this bench:

- Continue Step0 as a no-motion data-chain check and Step1 as a no-contact
  dry-run scaffold.
- Do not proceed to contact force-control under the label `1 kHz control` until
  the actuator/controller architecture that owns the `1 kHz` loop is chosen and
  measured.

## Files

- `config/straight_line_reference.json`: geometry, register map, gains, guards,
  and OnRobot baseline metrics.
- `tools/preflight_readonly.py`: Dashboard/RTDE/Kunwei connect-only preflight.
- `tools/kunwei_rtde_bridge.py`: Kunwei TCP parser, zeroing, logger, and RTDE
  input-register writer.
- `tools/analyze_kunwei_closed_loop_run.py`: run-summary analysis with the same
  runtime/contact/speed style gates as the OnRobot baseline.
- Local `programs/*.script` files are source copies; the deployed teach-pendant
  paths are `/programs/andyl/kunwei/*.script`.
- `step0_register_echo.script`: Step0 no-motion input-to-output register echo.
- `step0_no_contact_straight_10mm.script`: minimal no-contact 10 mm
  straight-line check with no Kunwei or RTDE input dependency.
- `step1_full_no_contact_pipeline.script`: full no-contact pipeline dry run
  with Kunwei bridge, software zero, guard, reference XY start, full XY line,
  and retract, but no contact search or force-control.
- `step2a_contact_search.script`: corrects to the old reference TCP orientation,
  moves to the old contact-start XY at current Z, then down to old contact Z +
  `15 mm`, then uses interruptible `speedl` base-Z descent until raw signed
  `Fz/normal_force <= -2 N` or `20 mm` search travel is exhausted. `force_norm`
  is a `50 N` guard, not the contact trigger.
- `step2b_closed_loop_pilot.script`: integrated contact search plus full-line
  closed-loop pilot. Ubuntu bridge target is `3 N`; tangent speed is `5 mm/s`.
- `step2c_closed_loop_full.script`: integrated contact search plus full-line
  final run. Ubuntu bridge target is `5 N`; tangent speed is `10 mm/s`.
- `step0_full_no_contact_pipeline.script`: older 10 mm no-contact pipeline
  source retained for comparison.
- `kunwei_register_echo.script`: older name for Step0 register echo, retained
  for comparison.
- `kunwei_no_contact_line.script`: current-pose no-contact XY line.
- `kunwei_contact_search.script`: low-speed base-Z contact trigger.
- `kunwei_closed_loop_line.script`: speedl closed-loop line.

## Reference Line

- Full reference start: `[0.444781266, 0.228663962, 0.020265571]`
- Full reference end: `[0.489249167, 0.180685371, 0.020619143]`
- Contact start: `[0.446585764, 0.226717001, 0.020279919]`
- Contact window: `2.69 mm` to `66.27 mm` progress
- XY unit vector: `[0.679764156, -0.733430768]`

The minimal scripts start from the current TCP pose and follow this XY
direction. Step1 moves to the reference XY start at the current TCP Z, then
follows the full reference XY line. Step2 is the first current script family
that uses the old contact-start Z, but only as an approach height offset:
`entry_z = 0.020279919 + 0.015 = 0.035279919 m`. Step2 first corrects to the
old reference TCP orientation `[rx, ry, rz] = [3.141572714, -0.000026465,
-0.000024158]`. Step2 does not `movel` straight to the old contact Z; it
reaches contact only through the low-speed interruptible search.

## Zero Policy

For Kunwei Step1 and Step2, use software zero in the Ubuntu bridge:

- Initial zero: `kunwei_rtde_bridge.py --baseline-s 5` averages the first
  no-motion window and subtracts it from all six axes.
- The current Step1/Step2 teach-pendant scripts do not write
  `output_double_register_34`. That avoids stale request counters and repeated
  in-run re-zero events.

Do not use these for Step1 unless a separate safety gate explicitly approves
them:

- UR `zero_ftsensor()`
- Kunwei tare/zero/filter/config commands
- UR TCP or payload writes

This keeps the dry run reversible and makes the sign problem visible in logs
instead of hiding it in a hardware-side bias.

## Older 10 mm No-Contact Line

This older reference program is retained for comparison. The current Step1
choice is `step1_full_no_contact_pipeline.script`, not this 10 mm script.

Required gate before running:

- Robot is at a confirmed no-contact height.
- Cable route has slack for at least 10 mm along the reference direction.
- Dashboard safety is `NORMAL`.
- Robot mode is `RUNNING`.
- Program is stopped/ready.
- Current TCP/payload are left as-is; do not write TCP or payload.

Teach pendant program:

```text
/programs/andyl/kunwei/step0_no_contact_straight_10mm.script
```

Expected result:

- TCP moves about `10 mm` along `[0.679764156, -0.733430768]` in XY.
- Tangent speed is `5 mm/s`.
- Fixed TCP orientation.
- Stop reason output register `30` becomes `1` for path complete.

If direction, cable slack, or clearance is questionable, stop here and do not
continue to Kunwei streaming or closed-loop force control.

## Step1: Full No-Contact Pipeline

Use this after Step0 echo/frequency has passed and you want the whole
non-contact scaffold in place before touching the surface. This is the final
contact-run shape, except it skips contact search and force control.

Ubuntu bridge:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/tools/kunwei_rtde_bridge.py \
  --allow-kunwei-stream-command \
  --write-rtde-inputs \
  --baseline-s 5 \
  --rezero-s 1 \
  --duration-s 60 \
  --target-force-n 3 \
  --normal-axis fz \
  --normal-sign 1
```

Teach pendant program:

```text
/programs/andyl/kunwei/step1_full_no_contact_pipeline.script
```

What this does:

- `0`: waits for Kunwei bridge sensor readout and initial software zero.
- `1`: moves to the old reference path-start XY at the current Z; it does not
  move to the old contact Z.
- `2`: settles briefly at path start. It does not request a second software
  zero, so `output_double_register_34` is not touched during Step1.
- `3`: keeps a simple F/T guard active from the existing thresholds.
- `4`: mimics the F/T search travel by descending `50 mm` in base Z with the
  guard active. It does not use a contact trigger.
- `5`: leaves the F/T control/PID stage in code but skips it. The `+5/-5 N`
  sign remains unresolved until contact-search evidence identifies the axis.
- `5.1`: moves the full `63.58 mm` reference line in the XY plane at `5 mm/s`,
  fixed Z. Step1 uses `movel` for the search-like descent, no-contact line, and
  retract so UR's planner handles smooth acceleration. Step2 will tune `speedl`
  separately for interruptible contact/closed-loop behavior.
- `6`: retracts `+50 mm` in Z while keeping XY unchanged.

Stop here if guard trips, direction is wrong, `sensor_ok` drops, or the
path-start move is not the expected safe no-contact move. Before pressing Play,
confirm the path-start area has at least `50 mm` safe downward clearance and
the final retract has at least `50 mm` upward clearance.

## Step2: Contact Search And Closed Loop

Step2 is split into three teach-pendant scripts so the contact evidence can
gate the force-control run.

First run Step2A only:

```bash
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step2-operator.sh bridge-search
```

Teach pendant program:

```text
/programs/andyl/kunwei/step2a_contact_search.script
```

What Step2A does:

- waits for `sensor_ok`;
- `movel`s the current TCP pose to the old reference orientation
  `[3.141572714, -0.000026465, -0.000024158]`;
- `movel`s to `[0.446585764, 0.226717001, current_z]` with that reference
  orientation;
- then `movel`s vertically to `[0.446585764, 0.226717001, 0.035279919]`;
- descends in base Z at `25 mm/s`, using `speedl`;
- stops with code `11` when signed `Fz/normal_force <= -2 N`;
- stops with code `8` and retracts back to the entry Z if `20 mm` search
  travel is exhausted with no contact;
- retracts upward to the search entry height when contact is found.

After Step2A identifies the axis/sign, start the pilot bridge with explicit
mapping:

```bash
NORMAL_AXIS=fz NORMAL_SIGN=-1 \
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step2-operator.sh bridge-pilot
```

Change `NORMAL_AXIS` and `NORMAL_SIGN` only if a newer Step2A result contradicts
the current sign evidence. The current manual upward-press evidence showed
zeroed Fz becomes negative, so the closed-loop bridge maps contact compression
with `NORMAL_SIGN=-1`. The pilot teach-pendant program is:

```text
/programs/andyl/kunwei/step2b_closed_loop_pilot.script
```

Step2B repeats the same approach and contact search, then runs the full
`63.58 mm` line at `5 mm/s` with target force from the bridge, normally `3 N`.
Normal Z correction is controller-side `speedl`; approach and final retract are
`movel`.

Current Step2 approach moves use `movel` at `80 mm/s`. The Step2A contact-search
`speedl` uses `25 mm/s` with acceleration `500 mm/s^2`; contact trigger is
single-axis signed `Fz <= -2 N`, with `force_norm > 50 N` as the total-force guard.
On contact trigger, Step2A retracts upward to the search entry height using
`speedl` instead of staying at the contact point.

Current Step2B/Step2C integrated search uses `15 mm/s` with search acceleration
`300 mm/s^2`. The closed-loop line keeps tangent speed `10 mm/s` for Step2C,
line acceleration `500 mm/s^2`, URScript `dt = 0.002`, `speedl_hold_s = 0.002`,
and Ubuntu bridge RTDE input writes at `125 Hz` by default. The URScript
normal-force controller is PI with light filtering:
`filtered = 0.25 * raw + 0.75 * previous`, `Kp = 0.0004`, `Ki = 0.00008`,
and integral clamp `[-10, 10] N*s`. Contact triggers and force guards still use
raw unfiltered register values.

Only after Step2B is accepted, run the final bridge and script:

```bash
NORMAL_AXIS=fz NORMAL_SIGN=-1 \
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/scripts/step2-operator.sh bridge-final
```

```text
/programs/andyl/kunwei/step2c_closed_loop_full.script
```

Step2C uses target force `5 N` from the bridge and tangent speed `10 mm/s`.

## Legacy Roadmap

The original Stage 1..6 list below is retained as a planning reference only.
For current teach-pendant selection, use the explicit `Step0`, `Step1`, and
`Step2` names above.

### Legacy Stage 1: Read-Only Preflight

Read-only command:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/tools/preflight_readonly.py
```

Pass criteria:

- Dashboard safety contains `NORMAL`.
- Robot mode is `RUNNING`.
- Program state is stopped or otherwise intentionally ready.
- RTDE reports the expected current TCP/payload; TCP is observed, not changed.
- Kunwei TCP endpoint accepts a connect-only probe.

### Legacy Stage 2: Kunwei No-Motion Zero And Bridge

Live sensor command and RTDE-write command:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/tools/kunwei_rtde_bridge.py \
  --allow-kunwei-stream-command \
  --write-rtde-inputs \
  --baseline-s 5 \
  --duration-s 30 \
  --target-force-n 3 \
  --normal-axis fz \
  --normal-sign 1
```

This sends Kunwei `48 AA 0D 0A` and final `43 AA 0D 0A`, and writes RTDE input
double registers. It does not move the robot. Use `--no-start-command` only if
the sensor is already streaming.

Pass criteria:

- About `1 kHz` Kunwei samples.
- `parse_errors = 0`.
- Baseline completes.
- Bridge writes about `125 Hz`.
- `sensor_ok` becomes `1` after baseline and stays live.

### Legacy Stage 3: RTDE Register Echo

Keep the bridge running. On the teach pendant, run:

```text
/programs/andyl/kunwei/step0_register_echo.script
```

Pass criteria:

- `bridge_rtde_125hz.csv` shows output registers `24..29` following the input
  values while the program is active.
- No motion occurs.

### Legacy Stage 4: No-Contact Line

At a safe no-contact height and with cable slack confirmed, run the bridge for
logging, then run:

```text
/programs/andyl/kunwei/kunwei_no_contact_line.script
```

Pass criteria:

- XY direction matches the reference direction.
- TCP speed is near the selected tangent speed.
- Kunwei force remains near the zero baseline.
- UR stop reason code is `1` for path complete.

### Legacy Stage 5: Contact Search

Run the bridge with the candidate normal axis/sign. Then run:

```text
/programs/andyl/kunwei/kunwei_contact_search.script
```

Pass criteria:

- The script stops at code `11` when `abs(normal_force) >= 2 N`.
- The logged raw `Fx/Fy/Fz` change identifies the real Kunwei normal axis and
  sign.
- There is no safety stop or guard stop.

### Legacy Stage 6: Closed-Loop Pilot And Final Repeats

Pilot bridge command:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/tools/kunwei_rtde_bridge.py \
  --allow-kunwei-stream-command \
  --write-rtde-inputs \
  --baseline-s 5 \
  --duration-s 25 \
  --target-force-n 3 \
  --normal-axis fz \
  --normal-sign -1 \
  --rtde-hz 125
```

For final repeats, set `--target-force-n 5` and update/deploy
`kunwei_closed_loop_line.script` only after the pilot confirms the
normal-velocity sign. The final tangent speed is `0.010 m/s`; the checked-in
program keeps `0.005 m/s` for the pilot.

Analyze each run:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/tools/analyze_kunwei_closed_loop_run.py \
  /path/to/run/bridge_rtde_125hz.csv
```

Acceptance output:

- Kunwei normal-force mean/std.
- `abs(|normal_force| - target)` statistics.
- XY path error statistics.
- URScript stop reason counts.
- OnRobot baseline reminder: `0.261 mm` mean 2D path error, `0.343 mm` P95,
  and about `0.623 N` contact-window `|Fz|-5` mean error.

## Abort Conditions

Any of these must stop the current stage:

- Sensor heartbeat stale over `80 ms`.
- `sensor_ok != 1` after baseline.
- `abs(normal_force) > 12 N`.
- `force_norm > 50 N` for Step2 contact search; older Step0/Step1 guards used
  `15 N`.
- `torque_norm > 0.6 Nm`.
- Normal correction travel over `8 mm`.
- Path progress outside the configured range.
- Runtime limit exceeded.
- Safety mode not normal.
- User stop or confusing bench state.

## Stop Reason Codes

- `1`: path complete
- `2`: heartbeat stale
- `3`: sensor not ok
- `4`: external stop request
- `5`: normal force guard
- `6`: force norm guard
- `7`: torque norm guard
- `8`: normal correction guard
- `9`: path progress guard
- `10`: runtime limit
- `11`: contact search trigger
