# Goal: Run OnRobot HEX-E 6 h Read-Only Bench Drift On Ubuntu

You are on Ubuntu `andy7`. Work inside:

`/home/andy/ur10e_ros2_ws/ft_sensor/archive/onrobot/hex_e_v2_3010007655`

Run the OnRobot HEX-E standalone bench drift experiment. The Compute Box should be connected to Ubuntu Ethernet `enp3s0`, and its fixed IP is `192.168.1.1`.

## Non-Negotiable Safety Rules

- Do not call any zero, bias, autocalib, firmware, DIP switch, or configuration endpoints.
- Do not install the sensor on UR10e.
- Do not connect the bare-wire #8 robot power cable.
- Do not modify the sensor or Compute Box state.
- Only read `/version` and the Socket.IO force/torque stream.

## EOAT v13 Context

The current EOAT design reference has been copied to:

`/home/andy/ur10e_ros2_ws/experiments/archive/onrobot/onrobot_hex_e_v2_3010007655/eoat_design/v13_ksm8n_receiver_5p3mm_side_window_85mm/`

The design metadata reports:

- `contact_point_from_flange_face_mm = 85.0`
- `tcp_on_centerline = true`
- source file: `verification.json`

For this standalone 6 h bench drift run, record this as context only. Do not
apply TCP, gravity, or payload compensation to the raw OnRobot drift data, and
do not write this TCP candidate into UR TCP settings, ROS2 launch files, or
control code. The printed EOAT has not yet been mounted on UR10e.

## Steps

1. Confirm environment:

   ```bash
   pwd
   python3 --version
   ip -br addr show enp3s0
   ping -c 4 192.168.1.1
   curl -s http://192.168.1.1/version
   ```

2. If `ping 192.168.1.1` fails, wait up to 10 minutes while the user checks Compute Box power, Ethernet cable, and `enp3s0`. Stop if it still fails after 10 minutes.

3. Create one timestamped run root:

   ```bash
   RUN_ROOT="$PWD/measurements/bench_drift_6h_$(date +%Y%m%d_%H%M)"
   mkdir -p "$RUN_ROOT"
   ```

4. Run a 2 minute read-only dry run:

   ```bash
   python3 tools/onrobot_socketio_logger.py \
     --host 192.168.1.1 \
     --duration-s 120 \
     --out-dir "$RUN_ROOT/dry_run_2min" \
     --status-every-s 30
   ```

5. Inspect `$RUN_ROOT/dry_run_2min/summary.json`. Proceed only if:

   - rows are increasing and nonzero;
   - `status` is `0`;
   - `authenticated` is `true` or `True`;
   - `bias` is `false` or `False`.

6. Start the 6 hour read-only run:

   ```bash
   python3 tools/onrobot_socketio_logger.py \
     --host 192.168.1.1 \
     --duration-s 21600 \
     --out-dir "$RUN_ROOT/main_6h" \
     --status-every-s 1800
   ```

7. During the run, report a concise status every 30 minutes using the logger output. Do not stop the run unless the user asks or the hardware/network becomes unrecoverable.

8. After the run, ensure these files exist:

   - `$RUN_ROOT/main_6h/raw_wrench.csv`
   - `$RUN_ROOT/main_6h/events.jsonl`
   - `$RUN_ROOT/main_6h/summary.json`
   - `$RUN_ROOT/main_6h/drift_10min_bins.csv`
   - `$RUN_ROOT/main_6h/force_torque.png`
   - `$RUN_ROOT/main_6h/run_notes.md`

9. Final response must include:

   - run directory path;
   - effective duration and row count;
   - reconnect/error count;
   - final `status`, `authenticated`, `bias`, and live serial;
   - force/torque plot path;
   - whether the run qualifies as full success or partial run.

## Acceptance

Full success means effective duration is at least 5.5 hours, outputs are generated, and no unrecoverable link loss appears in `events.jsonl`.
