# UR10e ROS 2 Workspace

This workspace is for local UR10e launch wrappers, calibration output, and
bench-specific configuration. The old UR5e workspace is intentionally left
unchanged.

## Current Bench Defaults

- robot type: `ur10e`
- robot IP: `192.168.1.18`
- Ubuntu direct Ethernet: `enp3s0`
- Ubuntu direct Ethernet IP: `192.168.1.10/24`
- local bringup package: `src/ur10e_bringup`
- calibration file: `src/ur10e_bringup/config/ur10e_calibration.yaml`

## Workspace Organization

Filename and folder cleanup is staged. New experiment folders should follow the
future layout in `docs/workspace-organization.md`; historical raw runs, vendor
backups, controller programs, generated docs, and ROS package paths should not
be moved for cosmetic cleanup.

This branch also serves as a dated UR10e materials archive for evidence gathered
from 2026-05-20 through 2026-06-02. See
`docs/materials-archive-20260520-20260602.md` before treating the contents as a
single clean experiment result.

## Build

```bash
source /opt/ros/humble/setup.bash
cd /home/andy/ur10e_ros2_ws
colcon build --symlink-install
```

## Data Analysis Python

Keep `/usr/bin/python3` for ROS, apt-backed tools, and live bench scripts that
depend on the system environment. Run UR10e reports and offline data analysis
through the repo-local conda prefix instead:

```bash
cd /home/andy/ur10e_ros2_ws
/home/andy/miniconda3/bin/conda env create \
  -p /home/andy/ur10e_ros2_ws/.conda/ur10e-data \
  -f /home/andy/ur10e_ros2_ws/environment-ur10e-data.yml
scripts/ur10e_data_python.sh weekly_meeting/analyze_three_stream_600s.py
```

For an existing environment, update it with:

```bash
/home/andy/miniconda3/bin/conda env update \
  -p /home/andy/ur10e_ros2_ws/.conda/ur10e-data \
  -f /home/andy/ur10e_ros2_ws/environment-ur10e-data.yml \
  --prune
```

The wrapper clears `PYTHONPATH`, disables user-site packages, and forces
Matplotlib's non-interactive backend so report scripts do not mix conda, apt,
ROS, and `~/.local` packages.

## Calibration

Do not reuse the UR5e calibration. After the UR10e is reachable at
`192.168.1.18`, generate a fresh file:

```bash
source /opt/ros/humble/setup.bash
ros2 launch ur_calibration calibration_correction.launch.py \
  robot_ip:=192.168.1.18 \
  target_filename:=/home/andy/ur10e_ros2_ws/src/ur10e_bringup/config/ur10e_calibration.yaml
```

## No-Motion Driver Smoke Test

Run this only after the fresh calibration exists and the UR10e Dashboard/RTDE
checks pass:

```bash
source /opt/ros/humble/setup.bash
source /home/andy/ur10e_ros2_ws/install/setup.bash
timeout --signal=INT 20s ros2 launch ur10e_bringup ur10e_control.launch.py launch_rviz:=false
```

The current Remote Control smoke default is headless and direct-link explicit:

```bash
source /opt/ros/humble/setup.bash
source /home/andy/ur10e_ros2_ws/install/setup.bash
timeout --signal=INT 20s ros2 launch ur10e_bringup ur10e_control.launch.py \
  robot_ip:=192.168.1.18 \
  reverse_ip:=192.168.1.10 \
  headless_mode:=true \
  activate_joint_controller:=false \
  launch_rviz:=false
```

Passing this smoke is the 5a0 gate. Step5a no-contact air motion and Step5b
contact remain separate live-gated stages.
