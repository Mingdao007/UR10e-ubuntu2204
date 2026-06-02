# Goal: Run OnRobot HEX-E 8 h Mounted Sensor-only Drift On Ubuntu

You are on Ubuntu `andy7`. Work inside:

`/home/andy/ur10e_ros2_ws/ft_sensor/onrobot/hex_e_v2_3010007655`

Run a mounted sensor-only OnRobot HEX-E drift experiment. The OnRobot HEX-E is
mounted on the UR10e wrist, but the UR10e must remain stationary and the sensor
must not contact the table, fixture, workpiece, or environment.

This replaces the older standalone `GOAL_RUN_ONROBOT_6H.md` flow. Do not use the
old "do not install the sensor on UR10e" assumption for this run.

## Non-Negotiable Safety Rules

- Do not execute UR motion programs.
- Do not call UR `zero_ftsensor()`.
- Do not call OnRobot zero, bias, autocalib, firmware, DIP switch, or
  configuration endpoints.
- Do not modify TCP, payload, gravity compensation, ROS, UR, or OnRobot config.
- Do not connect the bare-wire `#8 Robot Power Cable`.
- Only use the OnRobot Compute Box 24 V adapter for power.
- Only read OnRobot Compute Box `/version` and the Socket.IO force/torque stream.
- Stop before logging if the user has not confirmed: sensor is mounted,
  no-contact, cable is slack, and robot is stationary.

## Required User Facts Before Starting

Ask the user for these facts if they are not already present in the current
conversation:

- `P_work_drift`: six UR10e joint angles at the chosen work-near drift pose.
- Confirmation that the OnRobot sensor is not touching anything.
- Confirmation that the cable sweep around J4/J5/J6 passed or that the user
  visually checked the cable is slack.
- Confirmation that the Compute Box 24 V adapter has been plugged in.

Record these facts in the final report.

## Preflight

Read and record the current handoff:

`HANDOFF_READY_FOR_DRIFT_20260519.md`

Then run:

```bash
pwd
python3 --version
ip -br addr show enp3s0
ping -c 4 192.168.1.1
curl -sS --max-time 3 http://192.168.1.1/version
```

If `ping` or `/version` fails, wait for the user to check Compute Box power,
Ethernet cable, and Ubuntu wired interface. Do not continue to dry run until
`/version` is readable.

## Run Root

Create one timestamped run root:

```bash
RUN_ROOT="$PWD/measurements/mounted_sensor_only_drift_8h_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RUN_ROOT"
```

Write a short `preflight_notes.md` into `$RUN_ROOT` containing:

- `P_work_drift`
- no-contact confirmation
- cable slack / cable sweep result
- Compute Box power source
- `/version` response
- explicit note: no zero, no bias, no autocalib, no firmware, no config write

## Dry Run

Run a 2 minute read-only dry run:

```bash
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s 120 \
  --out-dir "$RUN_ROOT/dry_run_2min" \
  --status-every-s 30 \
  --flush-every-s 30
```

Inspect `$RUN_ROOT/dry_run_2min/summary.json`. Proceed only if:

- rows are nonzero and increasing;
- `status` is `0`;
- `authenticated` is `true` or `True`;
- `bias` is `false` or `False`.

If the dry run fails any gate, stop and report the exact failed field.

## Main 8 h Run

Start the 8 hour read-only run:

```bash
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s 28800 \
  --out-dir "$RUN_ROOT/main_8h" \
  --status-every-s 1800 \
  --flush-every-s 30
```

During the run:

- Report concise status every 30 minutes using logger output.
- Do not stop for ordinary Socket.IO polling reconnects if data continues.
- Stop only if the user asks, the connection becomes unrecoverable, or safety
  state changes.

## Post-run Checks

Ensure these files exist and are nonempty:

- `$RUN_ROOT/main_8h/raw_wrench.csv`
- `$RUN_ROOT/main_8h/events.jsonl`
- `$RUN_ROOT/main_8h/summary.json`
- `$RUN_ROOT/main_8h/drift_10min_bins.csv`
- `$RUN_ROOT/main_8h/force_torque.png`
- `$RUN_ROOT/main_8h/fz.png`
- `$RUN_ROOT/main_8h/fxy.png`
- `$RUN_ROOT/main_8h/run_notes.md`

If plots or summary are missing, rebuild them:

```bash
python3 tools/analyze_onrobot_drift.py "$RUN_ROOT/main_8h" --baseline-s 600 --bin-s 600
```

## Report

Write a Chinese report to:

`$RUN_ROOT/report.md`

The report must include:

- 实验目的
- mounted sensor-only 状态
- `P_work_drift` 六轴值
- OnRobot Compute Box power/network/version information
- dry run result
- 8 h main run summary
- embedded full force/torque, Fz, and fxy figures
- 30 min, 1 h, 2 h, 4 h, 6 h, 8 h checkpoint or 10 min bin summary
- `status`, `authenticated`, `bias`, reconnect count, error count
- Fz first/last/change and axis statistics
- conclusion and next step

Use these plot paths in the report body:

- `main_8h/force_torque.png`
- `main_8h/fz.png`
- `main_8h/fxy.png`

## Final Response

Final response must include:

- `RUN_ROOT`
- effective duration and row count
- Fz first value, last value, and change
- reconnect/error count
- final `status`, `authenticated`, `bias`, and live serial
- full/Fz/fxy plot paths
- `report.md` path
- whether this is full success or partial run

Full success means:

- main run effective duration is at least 7.5 h;
- `status=0`;
- `authenticated=true`;
- `bias=false`;
- main CSV and all three plots exist;
- Chinese `report.md` exists;
- no unrecoverable connection failure occurred.
