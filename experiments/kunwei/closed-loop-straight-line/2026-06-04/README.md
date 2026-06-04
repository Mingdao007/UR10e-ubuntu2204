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

## Who Sends What

Use a split-control workflow:

- Ubuntu command line starts preflight, Kunwei logging, software zero, and RTDE
  input-register bridge.
- The teach pendant remains the motion authority. You choose the reviewed
  Script program and press Play there.
- Do not send motion URScript from Ubuntu for Step0. Remote Control can support
  that technically, but it removes the clear human decision point we want here.

Operator command wrapper:

```bash
/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/scripts/step0-operator.sh preflight
```

Then use either:

```bash
/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/scripts/step0-operator.sh benchmark-freq
```

for the no-motion highest-frequency check, or:

```bash
/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/scripts/step0-operator.sh bridge-full
```

or, for Kunwei logging without RTDE input writes:

```bash
/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/scripts/step0-operator.sh bridge-minimal-log
```

The bridge commands ask for a typed confirmation before sending Kunwei stream
commands. After `bridge-full` is running, run
`programs/step0_full_no_contact_pipeline.script` from the teach pendant.

## Control Mode And Data Path

The intended Step0 mode is **TP-started, RTDE-assisted**:

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
/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/scripts/step0-operator.sh bridge-echo
```

Then run this on the teach pendant:

```text
programs/kunwei_register_echo.script
```

Pass condition: the bridge log shows output registers `24..35` changing with
the same values written into input registers `24..36`. Only after that passes
should `step0_full_no_contact_pipeline.script` be trusted to read Kunwei values.

## Step0 Highest-Frequency Benchmark

Before any new no-contact line or contact run, use the no-motion frequency
benchmark:

```bash
/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/scripts/step0-operator.sh benchmark-freq
```

Operator split:

- Ubuntu sends the Kunwei stream command and writes RTDE input registers.
- Teach pendant runs `programs/kunwei_register_echo.script`.
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

Do not describe the current Step0/closed-loop scaffold as `1 kHz` force
control.

Current motion-control implementation:

- Kunwei TCP frame parsing: sensor-side stream is expected at about `1 kHz`.
- Ubuntu bridge output to UR RTDE input registers: configured at `125 Hz`
  by `--rtde-hz 125`.
- URScript loop in the checked-in Step0 programs: `dt = 0.008`, so nominally
  `125 Hz`.
- Therefore, the active force-control input path is `125 Hz`, while the raw
  Kunwei log can still preserve `1 kHz` sensor data.

Why this is intentional for Step0:

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
- The checked-in Step0 bridge is deliberately configured at `125 Hz`.

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

- Continue Step0 only as a data-chain and dry-run scaffold.
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
- `programs/step0_no_contact_straight_10mm.script`: minimal no-contact 10 mm
  straight-line check with no Kunwei or RTDE input dependency.
- `programs/step0_full_no_contact_pipeline.script`: no-contact pipeline dry run
  with Kunwei bridge, software zero, guard, XY line, and retract, but no search
  or force-control.
- `programs/kunwei_register_echo.script`: no-motion input-to-output register echo.
- `programs/kunwei_no_contact_line.script`: current-pose no-contact XY line.
- `programs/kunwei_contact_search.script`: low-speed base-Z contact trigger.
- `programs/kunwei_closed_loop_line.script`: speedl closed-loop line.

## Reference Line

- Full reference start: `[0.444781266, 0.228663962, 0.020265571]`
- Full reference end: `[0.489249167, 0.180685371, 0.020619143]`
- Contact start: `[0.446585764, 0.226717001, 0.020279919]`
- Contact window: `2.69 mm` to `66.27 mm` progress
- XY unit vector: `[0.679764156, -0.733430768]`

The URScript programs start from the current TCP pose and follow this XY
direction. They do not move to the old absolute Z.

## Zero Policy

For Kunwei Step0, use software zero in the Ubuntu bridge:

- Initial zero: `kunwei_rtde_bridge.py --baseline-s 5` averages the first
  no-motion window and subtracts it from all six axes.
- Path-start zero: `step0_full_no_contact_pipeline.script` writes
  `output_double_register_34` with a request counter; the bridge then collects
  `--rezero-s` seconds of no-contact data and updates the software baseline.

Do not use these for Step0 unless a separate safety gate explicitly approves
them:

- UR `zero_ftsensor()`
- Kunwei tare/zero/filter/config commands
- UR TCP or payload writes

This keeps the dry run reversible and makes the sign problem visible in logs
instead of hiding it in a hardware-side bias.

## Step0-Minimal: No-Contact Line

Use this before the full Kunwei bridge and closed-loop plan. It is only a
10 mm straight-line check from the current TCP pose along the reference XY
direction.

Required gate before running:

- Robot is at a confirmed no-contact height.
- Cable route has slack for at least 10 mm along the reference direction.
- Dashboard safety is `NORMAL`.
- Robot mode is `RUNNING`.
- Program is stopped/ready.
- Current TCP/payload are left as-is; do not write TCP or payload.

Teach pendant program:

```text
programs/step0_no_contact_straight_10mm.script
```

Expected result:

- TCP moves about `10 mm` along `[0.679764156, -0.733430768]` in XY.
- Tangent speed is `5 mm/s`.
- Fixed TCP orientation.
- Stop reason output register `30` becomes `1` for path complete.

If direction, cable slack, or clearance is questionable, stop here and do not
continue to Kunwei streaming or closed-loop force control.

## Step0-Full: No-Contact Pipeline

Use this when the minimal line is already reasonable and you want the whole
non-contact scaffold in place before touching the surface.

Ubuntu bridge:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/tools/kunwei_rtde_bridge.py \
  --allow-kunwei-stream-command \
  --write-rtde-inputs \
  --baseline-s 5 \
  --rezero-s 1 \
  --duration-s 30 \
  --target-force-n 3 \
  --normal-axis fz \
  --normal-sign 1
```

Teach pendant program:

```text
programs/step0_full_no_contact_pipeline.script
```

What this does:

- `0`: waits for Kunwei bridge sensor readout and initial software zero.
- `1`: moves to the old reference path-start XY at the current Z; it does not
  move to the old contact Z.
- `2`: requests a second software zero at path start through
  `output_double_register_34`.
- `3`: keeps a simple F/T guard active from the existing thresholds.
- `4`: leaves the F/T search stage in code but skips it.
- `5`: leaves the F/T control/PID stage in code but skips it. The `+5/-5 N`
  sign remains unresolved until contact-search evidence identifies the axis.
- `5.1`: moves `10 mm` in the XY plane at `5 mm/s`, fixed Z.
- `6`: retracts `+5 mm` in Z while keeping XY unchanged.

Stop here if guard trips, direction is wrong, `sensor_ok` drops, or the
path-start move is not the expected safe no-contact move.

## Stage 1: Read-Only Preflight

Read-only command:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/tools/preflight_readonly.py
```

Pass criteria:

- Dashboard safety contains `NORMAL`.
- Robot mode is `RUNNING`.
- Program state is stopped or otherwise intentionally ready.
- RTDE reports the expected current TCP/payload; TCP is observed, not changed.
- Kunwei TCP endpoint accepts a connect-only probe.

## Stage 2: Kunwei No-Motion Zero And Bridge

Live sensor command and RTDE-write command:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/tools/kunwei_rtde_bridge.py \
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

## Stage 3: RTDE Register Echo

Keep the bridge running. On the teach pendant, run:

```text
programs/kunwei_register_echo.script
```

Pass criteria:

- `bridge_rtde_125hz.csv` shows output registers `24..29` following the input
  values while the program is active.
- No motion occurs.

## Stage 4: No-Contact Line

At a safe no-contact height and with cable slack confirmed, run the bridge for
logging, then run:

```text
programs/kunwei_no_contact_line.script
```

Pass criteria:

- XY direction matches the reference direction.
- TCP speed is near the selected tangent speed.
- Kunwei force remains near the zero baseline.
- UR stop reason code is `1` for path complete.

## Stage 5: Contact Search

Run the bridge with the candidate normal axis/sign. Then run:

```text
programs/kunwei_contact_search.script
```

Pass criteria:

- The script stops at code `11` when `abs(normal_force) >= 2 N`.
- The logged raw `Fx/Fy/Fz` change identifies the real Kunwei normal axis and
  sign.
- There is no safety stop or guard stop.

## Stage 6: Closed-Loop Pilot And Final Repeats

Pilot bridge command:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/tools/kunwei_rtde_bridge.py \
  --allow-kunwei-stream-command \
  --write-rtde-inputs \
  --baseline-s 5 \
  --duration-s 25 \
  --target-force-n 3 \
  --normal-axis fz \
  --normal-sign 1
```

For final repeats, set `--target-force-n 5` and edit
`programs/kunwei_closed_loop_line.script` only after the pilot confirms the
normal-velocity sign. The final tangent speed is `0.010 m/s`; the checked-in
program keeps `0.005 m/s` for the pilot.

Analyze each run:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04/tools/analyze_kunwei_closed_loop_run.py \
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
- `force_norm > 15 N`.
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
