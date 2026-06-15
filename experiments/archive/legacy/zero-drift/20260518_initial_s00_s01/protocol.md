# UR10e First Drift Protocol

Target network state:

- UR10e static IP: `192.168.1.18/24`
- Ubuntu direct link: `192.168.1.10/24` on `enp3s0`
- no gateway on the direct UR Ethernet connection

All commands below are no-motion except `zero_ftsensor()`, which only resets
the internal force-torque estimate and must be run with the tool not touching
the environment.

## 1. Read-Only Bring-Up

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/check_ubuntu_network.py
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/check_ur_interfaces.py
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/check_dashboard_state.py
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/read_payload_tcp_state.py
```

Proceed only if required ports `29999`, `30002`, and `30004` are open,
Dashboard remote control is true, and safety mode is normal.

## 2. S00 Raw Bias

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/sample_tcp_force.py \
  --seconds 60 --hz 20 --plot both \
  --prefix S00_raw_nozero_60s
```

## 3. S01 Zero Drift

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/measure_zero_drift_after_zeroft.py \
  --confirm-no-contact \
  --seconds 700 --hz 20 \
  --prefix S01_rezero_700s
```

## 4. S02 Thermal Drift

Run only if S01 is stable.

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/sample_thermal_drift.py \
  --zero-first --confirm-no-contact \
  --seconds 3600 --hz 20 \
  --prefix S02_rezero_3600s_thermal
```

## 5. ROS 2 Calibration And Smoke Test

Do not run the smoke test until calibration exists.

```bash
source /opt/ros/humble/setup.bash
ros2 launch ur_calibration calibration_correction.launch.py \
  robot_ip:=192.168.1.18 \
  target_filename:=/home/andy/ur10e_ros2_ws/src/ur10e_bringup/config/ur10e_calibration.yaml
```

```bash
source /opt/ros/humble/setup.bash
source /home/andy/ur10e_ros2_ws/install/setup.bash
timeout --signal=INT 20s ros2 launch ur10e_bringup ur10e_control.launch.py launch_rviz:=false
```
