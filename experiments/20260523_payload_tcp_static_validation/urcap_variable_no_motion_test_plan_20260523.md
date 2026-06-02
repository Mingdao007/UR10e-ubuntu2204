# URCap Variable No-Motion Test Plan

Date: 2026-05-23

Scope: plan only. No robot program, URScript, template, force node, zero, bias,
filter, speed, TCP, payload, or motion command has been executed by this plan.

## Current Evidence

- PolyScope OnRobot FT Setup discovery/binding is now verified by photos:
  - selected Compute Box IP: `192.168.1.1`
  - status: `OK`
  - sensor system health: `OK`
  - Compute Box version: `4.1.8`
  - UR robot IP: `192.168.1.18`
  - UR robot subnet: `255.255.255.0`
  - photo-read sensor serial: `HEX-EB806`
- PolyScope URCaps page shows `FT-OnRobot` version `4.1.7` by OnRobot A/S.
- OnRobot FT Setup TCP page shows `Use UR default TCP Configuration` selected;
  `Set from sensor flange` is not selected in the captured page.
- Manual `manual4UR.pdf` section `3.1 OnRobot Feedback Variables` states the
  following variables are updated automatically at approximately `125 Hz`:
  `F3D`, `Fx`, `Fy`, `Fz`, `T3D`, `Tx`, `Ty`, `Tz`, `bFT`,
  `of_compute_engine_ping`, `of_compute_engine_ping_max`, `of_return`, `tFT`.
- Follow-up photos from `2026-05-23`:
  - `IMG_1366.HEIC`: UR+ / OnRobot toolbar view after pressing the top-right
    UR+ button.
  - `IMG_1367.HEIC`: PolyScope `Variables` tab showing `F3D`, `Fx`, `Fy`,
    `Fz`, `T3D`, `Tx`, `Ty`, `Tz`, `bFT`, `of_return`, and `tFT` visible.
    The captured values are zero.
  - `IMG_1368.HEIC`: PolyScope `Variables` tab showing the same OnRobot
    variables with nonzero values. Approximate visible values include
    `F3D 0.27861`, `Fx 0.09775`, `Fy 0.07775`, `Fz -0.24904`,
    `T3D 0.00907`, `Tx -0.0053`, `Ty -0.005`, and `Tz 0.0059`.
  - `IMG_1369.HEIC`: OnRobot hand-guide toolbar with tool `Z` axis selected.
- Read-only direct Ethernet check after `IMG_1368`:
  - route: Ubuntu -> Compute Box `192.168.1.1:49151`
  - command class: `READCALIBRATIONINFO` once, then `READFT`
  - duration: `5 s`
  - errors: none
  - CSV:
    `/home/andy/ur10e_ros2_ws/experiments/20260523_payload_tcp_static_validation/onrobot_tcpdaq_after_urcap_variables/onrobot_tcpdaq_after_urcap_variables_5s_20260523_233147.csv`
  - row throughput: about `5193 Hz`
  - consecutive raw tuple change rate: about `250.5 Hz`

## Important Distinction

Seeing the variables in PolyScope proves URCap-side variable exposure. It does
not prove Ubuntu can log those variables through RTDE.

For Ubuntu to measure the actual update rate, one more mapping is needed:

```text
URCap variable -> UR program / script variable -> RTDE-readable output register
```

That mapping is not shown in the current photos.

## Phase A: PolyScope Variable Presence Check

Goal: prove the `FT-OnRobot 4.1.7` URCap exposes force/torque variables on this
bench.

Status: satisfied for variable visibility by `IMG_1367.HEIC`.

Remaining caveat: `IMG_1368.HEIC` proves nonzero values appear, but not the
actual PolyScope variable update rate. The 5 s Compute Box TCP DAQ run proves
direct Ethernet readout from the Compute Box, not direct RTDE readout of
PolyScope variables.

User side on teach pendant:

1. Confirm robot is stopped, safety is normal, end effector is not touching, and
   no person is touching the tool.
2. Do not press `F/T Zero`, `F/T Set TCP`, hand guide, force-control nodes, or
   OnRobot templates.
3. Follow the manual's no-motion variable example:
   - `Program Robot`
   - `Empty Program`
   - add only a `Wait` command
   - set `Wait` to `0.01 s`
   - press `Play`
   - open the `Variables` tab
4. Photograph the `Variables` tab if any of these names appear:
   `Fx`, `Fy`, `Fz`, `Tx`, `Ty`, `Tz`, `F3D`, `T3D`, `bFT`, `tFT`.

Codex side from Ubuntu:

1. Read Dashboard before and after.
2. Verify `Program running` returns to `false` after the short wait program.
3. Record any screenshots/photos and update this experiment record.

Success criterion:

- At least one photo clearly shows the OnRobot feedback variables in the
  PolyScope `Variables` tab.

Evidence:

- `IMG_1367.HEIC` satisfies this criterion for variable visibility.
- `IMG_1368.HEIC` extends this by showing nonzero OnRobot variable values.

Failure criterion:

- No OnRobot feedback variables appear, or the variables only appear after
  adding a template/force node. Stop and do not escalate automatically.

## Phase B: Ubuntu RTDE Rate Test Design

Goal: measure whether URCap variables can be logged from Ubuntu at about
`125 Hz`.

Current blocker:

- UR RTDE can log built-in fields like `actual_TCP_force`.
- PolyScope variables `Fx/Fy/Fz/...` are now visible, but there is no current
  evidence that they are directly available as RTDE outputs.
- A program may need to copy URCap variables into RTDE output registers before
  Ubuntu can log them.
- Separately, Ubuntu can directly read the Compute Box over Ethernet TCP DAQ,
  but that route reads the sensor/Compute Box directly rather than reading
  PolyScope variables.

No-motion candidate, not yet approved for execution:

1. Use a stopped/no-contact pose.
2. Run a no-motion program that only copies `Fx`, `Fy`, `Fz`, `Tx`, `Ty`, `Tz`
   into RTDE-readable output registers at a fixed interval.
3. Ubuntu logs those output registers through RTDE for `60 s`.
4. Analyze sample count, timing, and consecutive value-change rate.

Do not execute Phase B until the exact register mapping and program body are
written and reviewed.
