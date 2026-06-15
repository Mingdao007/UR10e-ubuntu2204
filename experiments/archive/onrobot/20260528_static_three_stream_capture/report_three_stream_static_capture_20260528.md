# UR10e / OnRobot Three-Stream Static Capture Report

Date: 2026-05-28

Objective: in a static no-motion robot state, compare three simultaneously
captured force/torque data routes:

1. UR10e built-in `actual_TCP_force` through RTDE.
2. OnRobot URCap / PolyScope variables exported through RTDE
   `output_double_register_24..29`.
3. OnRobot Compute Box high-speed UDP raw stream.

## Summary

The three-way separation is valid:

| Data route | Source | Expected behavior | Measured result |
| --- | --- | --- | --- |
| UR built-in force | `actual_TCP_force` over UR RTDE | `500 Hz` | `499.9999 Hz` / `500.0815 Hz` |
| OnRobot URCap variables | `output_double_register_24..29` over UR RTDE | about `125 Hz` | `125.1497 Hz` / `125.1200 Hz` |
| OnRobot Compute Box raw | high-speed UDP `49152` | default about `250 Hz`; `SPEED=2` about `500 Hz` | `249.9090 Hz` / `499.9997 Hz` |

Key result:

- With UDP `START/STOP` only, the simultaneous capture is effectively
  `UR 500 Hz + URCap 125 Hz + Compute Box raw 250 Hz`.
- After sending UDP `SPEED / 0x0082 = 2`, the simultaneous capture becomes
  `UR 500 Hz + URCap 125 Hz + Compute Box raw 500 Hz`.

These are not three equivalent force values. The URCap route is the
PolyScope/URCap variable口径. The UDP route is direct Compute Box raw/scaled
DAQ口径. Their absolute force offsets should not be compared as if they share
the same zero/reference/compensation.

## Safety Boundary

Both captures were static logging tests.

- Dashboard before and after: `PLAYING 3.urp`.
- Safety mode after both tests: `Safetymode: NORMAL`.
- Robot motion command: none.
- URScript command: none.
- UR TCP/payload write: none.
- OnRobot `FILTER`: not sent.
- OnRobot `BIAS`: not sent.
- OnRobot zero / `F/T Zero`: not sent.

The second run did send one OnRobot Compute Box UDP configuration command:
`SPEED / 0x0082 = 2`.

Measured robot speed evidence:

| Run | max linear `actual_TCP_speed` norm | max angular `actual_TCP_speed` norm |
| --- | ---: | ---: |
| START/STOP only | `9.19e-08` | `3.45e-07` |
| SPEED=2 | `1.22e-07` | `4.59e-07` |

These values are effectively zero for this no-motion check.

## Run A: START/STOP Only

This run did not write UDP `SPEED`. It only sent high-speed UDP `START`, then
final `STOP`.

Command log:

| Command | Hex | Data |
| --- | --- | ---: |
| `START` | `0x0002` | `3200` |
| `STOP` | `0x0000` | `0` |

Measured rates:

| Stream | Samples / rows | Measured rate | Timing notes |
| --- | ---: | ---: | --- |
| UR RTDE row stream | `2506` | `499.9999 Hz` | RTDE requested `500 Hz` |
| UR `actual_TCP_force` tuple updates | `2506` runs | `499.9999 Hz` | no repeated tuple holds |
| OnRobot URCap register tuple updates | `627` runs | `125.1497 Hz` | mostly 4 RTDE rows per URCap value |
| OnRobot UDP raw packets | `1252` | `249.9090 Hz` | `START/STOP` only default state |

UDP integrity:

| Field | Result |
| --- | --- |
| UDP status counts | `0: 1252` |
| UDP sequence deltas | `+1: 1251` |
| UDP sample counter delta modulo `65536` | `+4: 1251` |

Artifacts:

- Summary: `start_stop_only_expected_125_250_500/start_stop_only_expected_125_250_500_summary_20260528_040832.json`
- RTDE CSV: `start_stop_only_expected_125_250_500/start_stop_only_expected_125_250_500_rtde_ur500_urcap125_20260528_040832.csv`
- UDP CSV: `start_stop_only_expected_125_250_500/start_stop_only_expected_125_250_500_onrobot_udp_raw_20260528_040832.csv`

Interpretation:

This establishes the default simultaneous logging state. The UR and URCap data
come from one `500 Hz` RTDE stream: UR built-in force updates every RTDE row,
while OnRobot URCap registers update about every fourth RTDE row. The Compute
Box UDP stream, without a `SPEED` write in this run, delivered about `250 Hz`.

## Run B: SPEED=2 Then START/STOP

This run sent `SPEED / 0x0082 = 2` before starting the UDP stream. It did not
send `FILTER` or `BIAS`.

Command log:

| Command | Hex | Data |
| --- | --- | ---: |
| `SPEED` | `0x0082` | `2` |
| `START` | `0x0002` | `3200` |
| `STOP` | `0x0000` | `0` |

Measured rates:

| Stream | Samples / rows | Measured rate | Timing notes |
| --- | ---: | ---: | --- |
| UR RTDE row stream | `2511` | `500.0815 Hz` | RTDE requested `500 Hz` |
| UR `actual_TCP_force` tuple updates | per RTDE row | about `500 Hz` | no hold behavior expected |
| OnRobot URCap register tuple updates | about `627` runs | `125.1200 Hz` | still about 4 RTDE rows per URCap value |
| OnRobot UDP raw packets | `2509` | `499.9997 Hz` | `SPEED=2` enabled 500 Hz UDP output |

UDP integrity:

| Field | Result |
| --- | --- |
| UDP status counts | `0: 2509` |
| UDP sequence deltas | `+1: 2508` |
| UDP sample counter delta modulo `65536` | `+2: 2508` |

Artifacts:

- Summary: `speed2_expected_125_500_500/speed2_expected_125_500_500_summary_20260528_040921.json`
- RTDE CSV: `speed2_expected_125_500_500/speed2_expected_125_500_500_rtde_ur500_urcap125_20260528_040921.csv`
- UDP CSV: `speed2_expected_125_500_500/speed2_expected_125_500_500_onrobot_udp500_raw_20260528_040921.csv`

Interpretation:

This is the target simultaneous high-rate state. The UR built-in force remains
at about `500 Hz`, the OnRobot URCap/PolyScope variable export remains about
`125 Hz`, and the direct Compute Box UDP raw stream reaches about `500 Hz`.

## Practical Takeaways

Use the following labels in future experiment logs:

| Label | Meaning |
| --- | --- |
| `ur_rtde_actual_tcp_force_500hz` | UR internal force estimate through RTDE at `500 Hz` |
| `onrobot_urcap_registers_125hz` | OnRobot URCap / PolyScope variable口径 exported to RTDE registers |
| `onrobot_udp_raw_500hz` | Direct Compute Box UDP raw/scaled DAQ口径 after `SPEED=2` |

For analysis:

- Use RTDE `t_s` and UDP `t_s` as Ubuntu monotonic-clock timestamps for
  offline alignment.
- Treat URCap register values as 125 Hz samples carried inside a 500 Hz RTDE
  table.
- Do not upsample URCap values and call them new 500 Hz OnRobot samples.
- Do not compare UDP raw force offsets directly against URCap variables unless
  a separate zero/reference/compensation bridge is established.
