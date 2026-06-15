# UR5e KSM-8N ball-transfer tool v7

This version turns the successful direct-flange fit-check line into a complete
passive rolling-contact EOAT around the local `KSM-8N` M6 ball transfer.

## Current design

- Direct `UR5e` flange interface inherited from `v6_slim_adapter_72mm`
- Adapter OD: 72.0 mm
- Robot flange pattern: 4 x M6 on 50.0 mm PCD
- Center register/recess retained; OnRobot QC-R smooth locating pin is not copied
- Contact module: KSM-8N placeholder, ball diameter 8.0 mm
- KSM thread: M6 x 12.0 mm, held by captive M6 hex nut
- Contact point from flange face: 140.0 mm

## Output files

- `ur5e_ksm8n_ball_transfer_tool_v7_body.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v7_ksm_placeholder.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v7_assembly.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v7_onepiece_preview.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v7_receiver_fitcheck.step / .stl`

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
