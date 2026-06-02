# UR5e KSM-8N ball-transfer tool v11, flat-bottom recessed receiver face, 85 mm TCP

This version keeps the v9/v10 slim 28.0 mm receiver and changes the receiver
front face into a flat-bottom recessed surface. The KSM-8N remains a purchased
metal part; the recess removes the outer front layer to the receiver edge while
leaving a flat central seating boss for the KSM housing.

## Current design

- Direct `UR5e` flange interface inherited from `v6_slim_adapter_72mm`
- Adapter OD: 72.0 mm
- Robot flange pattern: 4 x M6 on 50.0 mm PCD
- Center register/recess retained; OnRobot QC-R smooth locating pin is not copied
- Contact module: purchased KSM-8N; placeholder is exported separately only for fit reference
- KSM thread: M6 x 12.0 mm, held by captive M6 hex nut
- Contact point from flange face: 85.0 mm
- Body segmentation: adapter 0-12 mm, base 12-32 mm, taper 32-46 mm, neck 46-56 mm, receiver 56-73.9 mm
- Receiver head OD: 28.0 mm
- Receiver front face: flat-bottom recess, 1.5 mm deep, with 18.0 mm central seating boss

## Output files

- `ur5e_ksm8n_ball_transfer_tool_v11_body.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v11_ksm_placeholder.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v11_assembly.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v11_onepiece_preview.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v11_receiver_fitcheck.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v11_flange_fitcheck.step / .stl`

`assembly` and `onepiece_preview` intentionally contain only the printable
body. The KSM-8N is a purchased metal part and is not fused into printable
geometry.

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
