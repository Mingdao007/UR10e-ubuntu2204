# EOAT v13 TCP Candidate Note

This package records the latest available printed-end-effector design found on
the Mac side:

`v13_ksm8n_receiver_5p3mm_side_window_85mm`

## Why This Is Included

Ubuntu-side agents that analyze UR10e / OnRobot data need access to the end
effector geometry before estimating TCP, payload, gravity consistency, or
force-frame alignment.

## TCP Candidate From Design Metadata

- Design metadata source: `v13_ksm8n_receiver_5p3mm_side_window_85mm/verification.json`
- Candidate contact point from flange face: `85.0 mm`
- Direction: along the tool centerline in the CAD design convention
- `tcp_on_centerline`: `true`

## Important Limits

- This is a design candidate, not a verified UR10e TCP setting.
- The end effector had not yet been mounted on UR10e when this package was
  prepared.
- Do not use this value to compensate the current OnRobot 6 h bench drift run.
- Do not write this value into UR TCP, ROS2 launch files, or control code until
  the real mounting stack is verified.

## What Must Be Confirmed Later

- Actual mounted orientation of the printed EOAT.
- Whether OnRobot HEX-E, Adapter Flange A, and any additional stack-up change
  the flange-to-contact distance.
- UR10e TCP frame direction and sign convention.
- Payload mass and center of mass after the real end effector stack is mounted.
- Whether the KSM-8N ball transfer is seated exactly as assumed by v13 CAD.

## Measured Print/Material Weight Evidence

Photo evidence from `2026-05-27` is recorded in:

`/home/andy/ur10e_lab_vault/onrobot/hex_e_v2_3010007655/photo_notes/20260527_printed_material_weights.md`

Filed Mac photo set:

`/Users/andyl/Documents/UR10e/ft_sensor/incoming_to_classify/20260527_printed_material_weights_20260527/`

Readings:

- Combined printed EOAT/material set: `70.5 g` (`IMG_1453.JPG`, confirmed by
  `IMG_1457.PNG` because the electronic balance display was flashing).
- White printed EOAT body with metal ball/top fastener: `51.5 g`
  (`IMG_1454.JPG`).
- Transparent printed small pieces: `19.0 g` (`IMG_1455.JPG`).
- Component sum: `70.5 g`.

These values are physical evidence for payload preparation only. They do not
establish center of mass, actual mounted orientation, cable contribution, or
authorization to write UR payload/TCP settings.
