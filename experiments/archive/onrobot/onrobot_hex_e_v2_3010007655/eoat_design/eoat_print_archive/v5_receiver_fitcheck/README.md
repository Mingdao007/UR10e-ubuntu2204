# v5 receiver fit-check coupons

Bench-only test coupons for comparing two M6 thread strategies before making
the full modular UR5e EOAT receiver.

Print these together with the existing UR5e flange fit-check:

- `/Users/andyl/Documents/UR5e/eoat/eoat_print/v4/ur5e_flat_pad_tool_v4_fitcheck.stl`

## Parts

- `v5_receiver_fitcheck_captive_m6_nut.stl`
  - Tests a captured M6 hex nut.
  - Insert an M6 hex nut into the hex pocket from the back/side.
  - Screw the KSM-8N or M6 ball plunger through the front clearance hole into
    the metal nut.

- `v5_receiver_fitcheck_self_thread_m6_pilots.stl`
  - Tests plastic self-threading / self-tapping style engagement.
  - It has three pilot holes: 5.0 mm, 5.2 mm, and 5.4 mm.
  - When viewing from the screw-entry front side, mark the holes before testing
    so the preferred pilot size is not lost.

## What to report back

- For the captive-nut coupon:
  - Does the M6 nut fit by hand, press-fit, or need filing?
  - Does the nut rotate when tightening?
  - Does the KSM / ball plunger engage smoothly?

- For the self-thread coupon:
  - Which pilot hole starts cleanly?
  - Which one holds tight without cracking?
  - Which one has the least wobble after one insertion/removal cycle?

## Boundary

These coupons are not robot-mounted parts. They are only for deciding the front
receiver thread strategy.
