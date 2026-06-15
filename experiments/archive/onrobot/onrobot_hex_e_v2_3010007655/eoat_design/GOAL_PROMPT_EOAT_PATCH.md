# Addendum For OnRobot 6 h Goal: EOAT v13 TCP Candidate

Use this addendum together with `GOAL_RUN_ONROBOT_6H.md` after this EOAT package
has been transferred into the Ubuntu experiment directory.

EOAT design package:

`eoat_design/v13_ksm8n_receiver_5p3mm_side_window_85mm/`

TCP candidate from design metadata:

- `contact_point_from_flange_face_mm = 85.0`
- Candidate direction: along the tool centerline in the CAD design convention
- Source: `eoat_design/v13_ksm8n_receiver_5p3mm_side_window_85mm/verification.json`

For the current OnRobot standalone 6 h bench drift run:

- Record this TCP candidate as context only.
- Do not apply TCP, gravity, or payload compensation to the raw OnRobot drift data.
- Do not write this value into UR TCP settings, ROS2 launch files, or control code.
- Do not install the EOAT during this bench drift run.

After the printed EOAT is physically mounted, discuss and verify:

- actual stack-up including OnRobot HEX-E and Adapter Flange A;
- TCP frame direction/sign convention;
- payload mass and center of mass;
- consistency between UR10e `actual_TCP_force` and external OnRobot wrench.
