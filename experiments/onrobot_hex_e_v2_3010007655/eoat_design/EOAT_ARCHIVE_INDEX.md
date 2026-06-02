# EOAT Archive Index

This folder records the full EOAT print-history archive for the current
OnRobot / UR10e experiment package.

## Locations

- Full archive on Ubuntu:
  `/home/andy/ur10e_ros2_ws/experiments/onrobot_hex_e_v2_3010007655/eoat_design/eoat_print_archive/`
- Active TCP candidate copy:
  `/home/andy/ur10e_ros2_ws/experiments/onrobot_hex_e_v2_3010007655/eoat_design/v13_ksm8n_receiver_5p3mm_side_window_85mm/`
- Manifest:
  `eoat_print_archive_manifest_20260519_1435.json`
- Checksum file:
  `eoat_print_archive_manifest_20260519_1435.sha256`

## Active Candidate

- Active design candidate: `v13_ksm8n_receiver_5p3mm_side_window_85mm`
- TCP candidate from v13 metadata: `85.0 mm` from flange face along the CAD
  tool centerline.
- Status: design candidate only. It had not been mounted on UR10e when this
  archive was prepared.

## What Not To Infer

- Historical versions in `eoat_print_archive/` are provenance and comparison
  evidence only.
- Do not treat older versions as current hardware merely because they are
  present in the archive.
- Do not apply the `85.0 mm` TCP candidate to OnRobot 6 h bench drift data.
- Do not write this TCP candidate into UR TCP settings, ROS2 launch files, or
  control code before the real mounting stack is verified.

## Later Verification

Before using the TCP candidate for control or compensation, verify:

- actual mounted EOAT orientation;
- OnRobot HEX-E and Adapter Flange A stack-up;
- UR10e TCP frame direction/sign convention;
- payload mass and center of mass;
- consistency between UR10e `actual_TCP_force` and the external OnRobot wrench.
