# Kunwei Gravity Axis Calibration 2026-06-11 00:15:23

## Scope

This run captures Kunwei raw force/torque frames and UR RTDE pose while the
operator manually rotates only wrist 3 through four quadrants.

No robot motion was commanded by the logger.
No UR RTDE inputs were written.
No UR `zero_ftsensor()` was called.
No Kunwei zero/tare/config command was sent.

## Segments

| segment | samples | wrist3 mean deg | Kunwei force mean N `[Fx,Fy,Fz]` | gravity in tool frame unit `[x,y,z]` |
|---|---:|---:|---|---|
| P0_wrist3_0_current | 4998 | -0.0 | `[10.56713, 3.59709, 0.83596]` | `[0.99083, 0.13438, 0.01389]` |
| P1_wrist3_plus_90 | 5050 | 90.0 | `[8.69137, 0.96184, 0.83670]` | `[0.13437, -0.99083, 0.01394]` |
| P2_wrist3_plus_180 | 5050 | 180.0 | `[6.19932, 2.84301, 0.84019]` | `[-0.99083, -0.13442, 0.01393]` |
| P3_wrist3_plus_270 | 5002 | 270.0 | `[7.97744, 5.38356, 0.84564]` | `[-0.13442, 0.99083, 0.01394]` |
| P4_return_P0 | 5036 | 0.0 | `[10.55774, 3.50844, 0.84944]` | `[0.99082, 0.13443, 0.01394]` |

## Drift Check

P4 minus P0 force drift:

`[-0.00939, -0.08865, 0.01347] N`

P4 minus P0 joint drift:

`[0.0021, 0.0004, 0.0011, 0.0017, -0.0032, 0.0005] deg`

This is good enough for the XY sign/permutation conclusion.

## Mapping Fit

The four-quadrant wrist3 sweep strongly supports no XY axis swap:

`F_T_xy ~= [+Fx_K, +Fy_K]` after subtracting constant offset.

Best centered signed-permutation fit:

| candidate | RMSE N | corr | fitted scale N | note |
|---|---:|---:|---:|---|
| `F_T = [+Fx_K, +Fy_K, -Fz_K]` | 0.04516 | 0.99938 | 2.22699 | Z sign not excited by this run |
| `F_T = [+Fx_K, +Fy_K, +Fz_K]` | 0.04516 | 0.99938 | 2.22699 | Z sign not excited by this run |
| current bridge `F_T = [+Fx_K, -Fy_K, -Fz_K]` | 1.28639 | -0.00660 | -0.01471 | fails XY gravity sweep |

Because `g_T_z` stayed near `0.014` for all four poses, this run does not
determine the sign of `Fz_K`.

## Conclusion

For XY:

`F_T_x = +Fx_K`

`F_T_y = +Fy_K`

The current bridge mapping `F_T_y = -Fy_K` is inconsistent with this gravity
axis calibration run.

For Z:

Run a separate pose that excites tool-Z gravity projection before changing
`F_T_z`.
