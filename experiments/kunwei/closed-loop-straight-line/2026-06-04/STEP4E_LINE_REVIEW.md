# Step4e Line Program Review Note

Generated on 2026-06-09 for the first straight-line paper-style outer-loop test after Step4d.

## TP Open Programs

- Preview, no motion: `/programs/andyl/kunwei/step4/step4e_preview_line_v1.urp`
- Contact hold: `/programs/andyl/kunwei/step4/step4e_contact_hold_line_v1.urp`
- Full line: `/programs/andyl/kunwei/step4/step4e_line_outerloop_v1.urp`

Each package has `.urp`, `.txt`, and `.script` uploaded to the same controller folder.

## Path

The line is defined by two Teach Pendant Move screenshots:

- Start/lower TCP pose: `[0.43301, 0.10802, -0.39179, 3.133, 0.529, 0.191]`
- End/higher TCP pose: `[0.49274, 0.23877, -0.38146, 2.968, 0.717, 0.007]`
- XY unit vector: `[0.415512246, 0.909587417]`
- XY length: `0.143751504 m`

Only the XY projection is used for the path. Z is not interpolated; normal motion is controlled by the force outer loop after contact latch.

## Control Boundary

- URScript owns the scaffold and consumes Cartesian `speedl` twist commands.
- Ubuntu bridge computes Step4e outer-loop commands from Kunwei zeroed wrench and RTDE actual TCP pose.
- IK remains inside the UR controller through Cartesian `speedl`.
- This is a medium-fidelity reproduction of the paper direction, not a strict joint-space finite-time RNN implementation.

## Register Map

- Existing force/safety inputs: input double registers `24..36`
- Step4e command inputs: input double registers `37..47`
- Command twist:
  - `37`: vx m/s
  - `38`: vy m/s
  - `39`: vz m/s
  - `40`: wx rad/s
  - `41`: wy rad/s
  - `42`: wz rad/s, expected zero
  - `43`: command valid
  - `44`: path progress m
  - `45`: force error N
  - `46`: orientation error rad
  - `47`: controller state

## Safety Defaults

- Search: deterministic downward `speedl` at `3 mm/s`, acceleration `300 mm/s^2`, no force admittance before contact latch
- Contact latch: normal force `<= -1 N` or force norm `> 1.5 N`
- Target force: `5 N`
- Raw normal guard: `20 N`
- Force norm guard: `50 N`
- Torque norm guard: `0.6 Nm`
- Sensor stale limit: `100 ms`
- Recoverable stop: retract `10 mm`, then return to the TP start pose if the robot can still move

## Local Entry Points

- `scripts/step4e-preview-v1-autowatch.sh`
- `scripts/step4e-hold-v1-autowatch.sh`
- `scripts/step4e-line-v1-autowatch.sh`

Autowatch waits for the exact expected TP program to be running before starting Kunwei streaming or writing RTDE input registers.
