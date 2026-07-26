# UR5e KSM-8N ball-transfer tool v8, 85 mm TCP

This version shortens the v7 complete KSM-8N tool into the first-print
candidate length. It keeps the same direct-flange interface and removable
captive-nut receiver, but moves the TCP to 85 mm from the flange face.

## Current design

- Direct `UR5e` flange interface inherited from `v6_slim_adapter_72mm`
- Adapter OD: 72.0 mm
- Robot flange pattern: 4 x M6 on 50.0 mm PCD
- Center register/recess retained; OnRobot QC-R smooth locating pin is not copied
- Contact module: KSM-8N placeholder, ball diameter 8.0 mm
- KSM thread: M6 x 12.0 mm, held by captive M6 hex nut
- Contact point from flange face: 85.0 mm
- Body segmentation: adapter 0-12 mm, base 12-32 mm, taper 32-46 mm, neck 46-56 mm, receiver 56-73.9 mm

## Output files

- `ur5e_ksm8n_ball_transfer_tool_v8_body.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v8_ksm_placeholder.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v8_assembly.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v8_onepiece_preview.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v8_receiver_fitcheck.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v8_flange_fitcheck.step / .stl`

## Receiver notes

- M6 hex pocket follows the v5 coupon: 10.6 mm across flats and 5.8 mm deep.
- The side window is for inserting the nut and optionally adding hot glue.
- Hot glue is retention only; the load path is printed receiver -> metal M6 nut -> KSM-8N thread.
- Keep glue away from the M6 thread and KSM bearing body.

## Print and bench order

1. Print `receiver_fitcheck`.
2. Check that the M6 nut inserts, does not rotate, and accepts the KSM-8N smoothly.
3. Print `body`.
4. Mount on the bare UR5e flange and perform a low-speed no-contact posture sweep.
5. Only then proceed to contact experiments.
