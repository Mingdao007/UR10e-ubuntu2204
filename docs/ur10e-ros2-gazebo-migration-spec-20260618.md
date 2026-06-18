# UR10e ROS2/Gazebo Migration Engineering Spec

## Scope

This document defines the current offline/no-motion engineering surface for the
UR10e ROS2 Remote Control/headless migration and the first Gazebo/offline
simulation MVP.

Current route:

- ROS2 Remote Control/headless is the current route.
- TP, URCap, and legacy bridge are archive/reference unless explicitly routed.
- Step5b live contact remains locked after the 2026-06-18 table-vibration
  feedback.

## Source-Of-Truth Order

1. `/home/andy/codex-private-skills-shared-main/skills/ur10e-remote-control/references/current-remote-stage.md`
2. `/home/andy/.codex/context/ur-contact-force-frame-contract.md`
3. `/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/config/local_control_textbook_spec.json`
4. `/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/config/step5_safe_frame.json`
5. `/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/config/step5_stage_table.json`
6. Audited handoffs under `/home/andy/codex_handoffs/`
7. Raw run artifacts under `/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/`

When sources disagree, do not silently pick one. Record the conflict in the
artifact or handoff and route it to the user only if it blocks offline progress.

## Simulation Boundary

Allowed by default:

- pure offline Python simulation;
- SDF/world/launch file generation;
- `ros2 launch --show-args`;
- short headless Gazebo smoke with timeout;
- tests, py_compile, JSON/XML/SDF sanity checks.

Disallowed unless explicitly re-authorized:

- real UR trajectory/action goals;
- UR driver live motion;
- bridge start;
- TP Play/upload;
- URScript send;
- `zero_ftsensor()`;
- Kunwei tare/config writes;
- payload/TCP/safety/controller setting writes.

Real-robot read-only diagnostics may be used only when needed. Failure of
read-only robot reachability cannot block Gazebo/offline simulator work.

## Artifact Schema Rules

Every new simulation artifact must name:

- `schema`;
- `mode`, for example `offline_no_motion`;
- `live_robot_command_authorized`;
- source paths for safe frame, stage table, calibrated URDF, launch/world;
- frame names and units;
- whether calibrated URDF was used or a nominal fallback was used;
- run status and acceptance fields.

Dense raw traces can be stored under `runs/`; durable summaries and decisions
belong in `docs/` or `/home/andy/codex_handoffs/`.

## Current MVP

Implemented MVP surfaces:

- `ur10e_example_controllers.step5b_simulation_mvp`
- `step5b_simulation_mvp` console script
- `launch/step5b_simulation_mvp.launch.py`
- `worlds/step5_table_world.sdf`
- `experiments/tase-contact-reproduction/tests/test_step5b_simulation_mvp.py`

The MVP intentionally does not depend on `gz_ros2_control` or
`gazebo_ros2_control`, because those packages were not present during discovery.
It uses the installed `ros_gz_sim` stack for a headless world/URDF launch
skeleton and pure Python for the first preposition and force-evidence artifact.

Discovery on 2026-06-18 found:

- present: `ros_gz`, `ros_gz_bridge`, `ros_gz_sim`, `ur_description`,
  `ur_robot_driver`, `controller_manager`, `robot_state_publisher`;
- missing: `gz_ros2_control`, `ign_ros2_control`, `gazebo_ros`,
  `gazebo_ros2_control`;
- `gz sim --versions` reported `8.12.0`.

The launch must run Gazebo server-only when `headless:=true` by passing `-s
--headless-rendering`. It must also sanitize the calibrated URDF before
publishing `/robot_description`: the source URDF is retained evidence, but the
simulation description strips the real `ros2_control` block containing
`ur_robot_driver/URPositionHardwareInterface`.

Simulation artifacts must explicitly record:

- frames: `gazebo_world=world`, `robot_base=base`, `robot_base_link=base_link`,
  `tool=tool0`, `safe_frame_basis=step5_safe_frame_base_xy`, and force vectors
  in `base`;
- units: `m`, `m/s`, `m/s^2`, `m/s^3`, `N`, `Nm`, `s`, and `rad`;
- expected contact geometry: surface name, center, top Z, size, safe-frame
  origin, `reaction_normal`, and `approach_normal`;
- sanitized URDF usage and source-path provenance.

Known environment blocker:

- Sourcing the full workspace overlay currently exposes a stale
  `install/ur10e_step5d_remote` prefix without a valid ament package index.
  `ros_gz_sim` package discovery can fail with `package 'ur10e_step5d_remote'
  not found`. This is an overlay cleanup/rebuild issue, not Step5b live
  authorization and not evidence that the simulator should touch the robot.
- A clean package environment using only `/opt/ros/humble` plus
  `install/ur10e_example_controllers/share/ur10e_example_controllers/package.bash`
  starts Gazebo server-only. With `spawn_robot:=true`, `ros_gz_sim create`
  may print its internal 5-second create-service timeout before the same
  request returns `OK creation`; record this as a smoke-test warning, not a
  live blocker.

## Stage 22 Low-Vibration Rule

The rejected live runner used repeated short action goals during Stage 22. The
offline replacement prototype must:

- produce one continuous time-parameterized trajectory artifact;
- report `goal_count`;
- report TCP delta, velocity, acceleration, and jerk metrics;
- keep Step5 safe-frame origin mapping from `step5_safe_frame.json`;
- keep live Step5b locked until a future explicit approval.

## Validation Layers

Minimum validation before claiming completion:

- repo doctor before mutation;
- unit tests for simulation MVP;
- local-control textbook alignment gate;
- contact semantic gate;
- Step5b authorization status gate when Step5b runner/authorization surfaces
  are touched;
- `colcon build --packages-select ur10e_example_controllers --symlink-install`;
- `ros2 launch ur10e_example_controllers step5b_simulation_mvp.launch.py --show-args`;
- clean-package Gazebo server smoke with timeout, because the full overlay has
  the stale `ur10e_step5d_remote` blocker described above;
- `git diff --check`.
