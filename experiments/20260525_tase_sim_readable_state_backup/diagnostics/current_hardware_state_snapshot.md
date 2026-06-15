# UR10e / OnRobot Current Hardware State

Last updated: 2026-05-24

Scope: current UR10e bench with OnRobot HEX-E v2 device identity
`hex_e_v2_3010007655`.

## Source Of Truth

This file is the source of truth for current UR10e/OnRobot hardware facts.
Do not infer the currently installed URCap from old session notes, disabled
backups, or local installer directories.

## Confirmed Current State

- Current actual installed URCap family: `ft-onrobot`.
- Current actual installed URCap version shown in PolyScope URCaps page:
  `FT-OnRobot` version `4.1.7` by OnRobot A/S.
- Current actual installed URCap is not unified OnRobot `6.5`, `6.4`, or
  `6.2`.
- Local OnRobot `6.5.3`, `6.4.1`, `6.3.3`, and `6.2.1` packages are candidate
  installers only. Their presence does not mean they are installed on the
  robot.
- UR10e IP: `192.168.1.18`.
- URSoftware: `5.11.9.1010452`.
- OnRobot Compute Box IP: `192.168.1.1`.
- OnRobot Compute Box observed web version: `4.1.8`.
- PolyScope OnRobot FT Setup discovery photo evidence from `2026-05-23`
  (`IMG_1358.HEIC` and `IMG_1362.HEIC`, filed on Mac under
  `Documents/UR10e/ft_sensor/incoming_to_classify/20260523_urcap_discovery_onrobot_ft_setup/`)
  shows selected IP address `192.168.1.1`, status `OK`, sensor system health
  `OK`, Compute Box version `4.1.8`, UR robot IP `192.168.1.18`, UR robot
  subnet mask `255.255.255.0`, and photo-read serial number `HEX-EB806`.
- PolyScope OnRobot FT Setup mounting/TCP photo evidence from `2026-05-23`
  (`IMG_1359.HEIC` and `IMG_1360.HEIC`) shows the device is mounted on the
  OnRobot Quick Changer and `Use UR default TCP Configuration` is selected;
  `Set from sensor flange` is not selected in the captured page.
- PolyScope UR+ / OnRobot toolbar and Variables photo evidence from
  `2026-05-23` (`IMG_1366.HEIC` and `IMG_1367.HEIC`, filed in the same Mac
  folder) shows OnRobot toolbar access from the UR+ button and the PolyScope
  `Variables` tab listing `F3D`, `Fx`, `Fy`, `Fz`, `T3D`, `Tx`, `Ty`, `Tz`,
  `bFT`, `of_return`, and `tFT`. The captured values are zero, so this proves
  variable visibility, not live update frequency.
- Follow-up PolyScope Variables evidence from `2026-05-23` (`IMG_1368.HEIC`,
  filed in the same Mac folder) shows the same OnRobot variables with nonzero
  values, including approximate visible values `F3D 0.27861`, `Fx 0.09775`,
  `Fy 0.07775`, `Fz -0.24904`, `T3D 0.00907`, `Tx -0.0053`, `Ty -0.005`,
  and `Tz 0.0059`. This proves the PolyScope variables are populated, but does
  not by itself prove their update rate.
- Follow-up OnRobot toolbar evidence from `2026-05-23` (`IMG_1369.HEIC`, filed
  in the same Mac folder) shows the hand-guide toolbar with the tool `Z` axis
  selected. This is a hand-guide axis/compliance UI state, not an Ethernet data
  logging setting.
- No verified current evidence identifies a visible green hand-guide enable /
  power control or a visible `Zero` control in the user's captured UI. Do not
  assume those controls exist or instruct the user to press them unless later
  photo/manual evidence identifies the exact control.
- Read-only Ubuntu-to-Compute-Box TCP DAQ after the URCap variable check:
  `/home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260523_payload_tcp_static_validation/onrobot_tcpdaq_after_urcap_variables`.
  The 5 s run sent only `READCALIBRATIONINFO` and `READFT` to
  `192.168.1.1:49151`, had no errors, row throughput about `5193 Hz`, and raw
  tuple consecutive-change rate about `250.5 Hz`. Treat this as a short direct
  Ethernet read confirmation, not yet as a stable long-run sensor update rate.
- Follow-up read-only Ubuntu-to-Compute-Box TCP DAQ in the same
  post-URCap/toolbar state:
  `/home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260523_onrobot_tcpdaq_post_urcap_10min`.
  The 600 s run sent only `READCALIBRATIONINFO` and `READFT` to
  `192.168.1.1:49151`, had no errors, returned `status=0` throughout, produced
  `3453086` PC request-response rows over `599.997556 s`, and measured raw
  tuple transition rates of about `251.9 Hz` at 30 s, `251.5 Hz` at 120 s, and
  `251.5 Hz` at 600 s. Treat this as the current measured direct TCP DAQ raw
  tuple new-value rate for this post-URCap/toolbar state, not as a global
  OnRobot sensor frequency and not as a measured PolyScope URCap variable rate.
  Dashboard before and after this run both reported `Program running: false`
  and `programState: STOPPED <unnamed>`, so the `~251.5 Hz` raw tuple rate did
  not require a `wait 0.01` UR program to be running during the measurement.
  It remains unverified whether a prior brief URCap/program action latched this
  higher-rate state.
- Force-value caveat from `2026-05-24`: the user observed PolyScope
  `Variables` `Fz` around `0.04 N`, while a same-state 5 s read-only TCP DAQ
  check at
  `/home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260524_onrobot_tcp_vs_polyscope_variables/onrobot_tcpdaq_current`
  still returned `Fz mean = -32.7114 N`, raw tuple transition rate
  `249.9168 Hz`, and Dashboard `Program running: false` /
  `programState: STOPPED <unnamed>`. Treat TCP DAQ `READFT` force values and
  PolyScope OnRobot Variables as different zero/reference/compensation
  口径 until proven otherwise. The `~250 Hz` TCP DAQ frequency result does not
  prove that TCP DAQ force values match PolyScope variable force values.
- Program-running comparison from `2026-05-24`: after the user started the
  no-motion `wait 0.01` program, Dashboard readback showed `Program running:
  true` and `programState: PLAYING <unnamed>`. RTDE `actual_TCP_force` was near
  zero (`Fz` observed around `0.30 N` in one read), but a simultaneous 5 s
  direct TCP DAQ check at
  `/home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260524_onrobot_tcp_vs_polyscope_variables/onrobot_tcpdaq_program_running`
  still returned `Fz mean = -32.6699 N` and raw tuple transition rate
  `250.5219 Hz`. Running the no-motion wait program therefore did not make TCP
  DAQ `READFT` force values match RTDE/PolyScope-variable force values.
- RTDE output-register export path from `2026-05-24`: Ubuntu can read
  `output_double_register_24..29` over RTDE; before any PolyScope-side mapping,
  those registers read as zero. Planned mapping for OnRobot URCap variables is
  `24=Fx`, `25=Fy`, `26=Fz`, `27=Tx`, `28=Ty`, `29=Tz`, read by
  `/home/andy/codex-private-skills/skills/ur10e-onrobot-hex/scripts/sample_urcap_ft_rtde_registers.py`.
  This is the preferred next path for exporting the near-zero PolyScope
  Variables to Ubuntu, but it still requires a manual no-motion PolyScope
  program edit to write the registers.
- Official OnRobot USB backup from `2026-05-24`:
  `/home/andy/ur10e_ros2_ws/ft_sensor/archive/onrobot/hex_e_v2_3010007655/vendor_usb_backups/onrobot_usb_20260524`
  with manifest
  `/home/andy/ur10e_ros2_ws/ft_sensor/archive/onrobot/hex_e_v2_3010007655/vendor_usb_backups/onrobot_usb_20260524_file_manifest.tsv`.
  The backup contains `483` regular files and occupies about `414M`. Offline
  inspection of the official `FT-OnRobot-4.1.7.1754.urcap` confirms
  `Fx/Fy/Fz/Tx/Ty/Tz/F3D/T3D/bFT/tFT` are URScript variables, with
  `tFT=[Fx,Fy,Fz,Tx,Ty,Tz]`. The official USB examples and scripts did not
  contain a ready-made `write_output_*register` or `output_*_register` mapping
  for Ubuntu RTDE export, so the manual no-motion output-register mapping above
  remains the preferred path.
- Current topology: UR10e, OnRobot Compute Box, and Ubuntu are all reachable
  through the small Ethernet switch.
- Switch box photo evidence from `2026-05-23` (`IMG_1343.HEIC`, filed on Mac
  under `Documents/UR10e/ft_sensor/incoming_to_classify/20260523_switch_box_ugreen_5port_gigabit/`)
  shows a UGREEN `HD/FD` five-port gigabit Ethernet switch labeled
  `10/100/1000Mbps`, auto-negotiated ports, no configuration required, and
  plug-and-play use.

## Current User-Applied UR TCP / Payload

Read-only RTDE readback on `2026-05-23` after the user clicked PolyScope
`Finish` for the payload wizard:

- Payload: `0.44 kg`.
- Payload CoG: `[0.005, -0.005, 0.025] m`.
- TCP offset: `[0.0, 0.0, 0.12254, 0.0, 0.0, 0.0]` m/rad.
- Interpretation: current temporary working configuration for no-motion and
  low-risk validation only. It is not yet validated for contact experiments or
  force-control use.

## Safety Boundary

- No robot motion.
- No `zero_ftsensor()` / OnRobot bias / F/T Zero.
- No further TCP or payload writes without an explicit user-approved step.
- No vendor template program execution.
- No URCap install, uninstall, or Compute Box setting change unless the user
  explicitly approves that exact operation in a later session.

## Not Yet Verified

- Whether the visible PolyScope OnRobot variables update at the documented
  rate during a no-motion program.
- Whether a no-motion PolyScope program can successfully write the visible
  OnRobot variables into `output_double_register_24..29` at a useful rate for
  Ubuntu logging.
- URCap 125 Hz variable link on this bench.
- Whether the post-URCap/toolbar TCP DAQ `~251.5 Hz` raw tuple change rate
  persists across reboot, toolbar state changes, or fresh power-up, and what
  exact state caused the difference from earlier `~100 Hz` TCP DAQ runs.
- Whether the higher-rate TCP DAQ state is activated by opening the URCap
  toolbar/variables, by briefly running a no-motion UR program, or by another
  Compute Box/URCap state transition.
- Why PolyScope OnRobot Variables can show near-zero Fz while direct TCP DAQ
  `READFT` reports about `-32 N` in the same stopped/no-motion state; likely
  candidates include different bias/zero handling, gravity/mounting
  compensation, coordinate/reference frame processing, or URCap variable-layer
  processing.
- Low-priority hand-guide issue: the toolbar axis buttons are visible and `Z`
  could be selected, but the user reported that hand guiding still did not
  move the robot even after pressing the small black button near the top power
  area. This is not part of the current data-logging path and should be
  investigated separately before any hand-guide use.
