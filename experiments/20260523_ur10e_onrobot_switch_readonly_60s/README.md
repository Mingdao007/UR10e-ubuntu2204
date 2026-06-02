# UR10e + OnRobot Switch Read-Only 60 s Test

Date: 2026-05-23

## Setup

- Network topology: UR10e control box Ethernet, OnRobot Compute Box Ethernet, and Ubuntu `enp3s0` connected through the small switch.
- Ubuntu interface: `enp3s0`, `192.168.1.10/24`.
- UR10e IP: `192.168.1.18`.
- OnRobot Compute Box IP: `192.168.1.1`.
- OnRobot Compute Box version: `4.1.8`.

## Safety Boundary

- UR10e was powered on.
- No robot motion was commanded.
- No program was run.
- No freedrive, zero, payload write, TCP write, URScript motion, or OnRobot bias/filter/speed command was sent.
- UR10e TCP/payload were read only.
- OnRobot sampling used `READCALIBRATIONINFO` once and repeated `READFT`.

## Pre/Post UR State

- Safety mode: `NORMAL`.
- Robot mode: `RUNNING`.
- Program state: `STOPPED <unnamed>`.
- Remote Control: `false`.
- PolyScope/URSoftware: `5.11.9.1010452`.
- Payload: `0.009 kg`.
- Payload CoG: `[0.0, 0.0, 0.0]`.
- TCP offset: `[0.0, 0.0, 0.0, 0.0, 0.0, 0.0]`.

Current payload and TCP are effectively not calibrated for the mounted EOAT, so UR internal force values are useful for read-only diagnostics only.

## 60 s Sampling Results

### UR10e RTDE `actual_TCP_force`

- Command: `sample_tcp_force.py --seconds 60 --hz 500`.
- Samples: `29999`.
- Achieved rate: about `500 Hz`.
- Mean receive interval: `2.000 ms`, median `2.000 ms`, p99 `2.085 ms`.
- CSV: `ur_rtde_actual_tcp_force/ur10e_actual_tcp_force_readonly_60s_20260523_194920.csv`.
- Plots:
  - `ur_rtde_actual_tcp_force/ur10e_actual_tcp_force_readonly_60s_20260523_194920.png`
  - `ur_rtde_actual_tcp_force/ur10e_actual_tcp_force_readonly_60s_20260523_194920_fz.png`
  - `ur_rtde_actual_tcp_force/ur10e_actual_tcp_force_readonly_60s_20260523_194920_fxy.png`

| Field | Mean | Min | Max | Range | Std |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fx_N | -0.575 | -1.976 | 0.846 | 2.822 | 0.295 |
| Fy_N | -1.676 | -2.692 | -0.680 | 2.012 | 0.243 |
| Fz_N | -9.753 | -10.818 | -8.711 | 2.107 | 0.319 |
| F_norm_N | 9.920 | 8.844 | 11.006 | 2.161 | 0.323 |
| Tx_Nm | 0.136 | 0.098 | 0.170 | 0.072 | 0.009 |
| Ty_Nm | -0.001 | -0.034 | 0.031 | 0.065 | 0.009 |
| Tz_Nm | -0.016 | -0.037 | 0.006 | 0.043 | 0.005 |

### OnRobot TCP DAQ

- Command: `sample_onrobot_tcp_daq.py --host 192.168.1.1 --seconds 60`.
- Samples/rows: `340426`.
- Request/response row rate: about `5674 Hz`.
- Minimum observed request interval: `0.147 ms`, equivalent to about `6781 Hz` instantaneous request rate.
- Consecutive raw tuple change rate: about `100.3 Hz`.
- Unique raw tuples: `6006`.
- CSV: `onrobot_tcpdaq_readonly/onrobot_tcpdaq_readonly_60s_20260523_194819.csv`.
- Raw tuple analysis: `onrobot_tcpdaq_readonly/onrobot_tcpdaq_readonly_60s_20260523_194819_raw_tuple_change_analysis.json`.
- Frequency diagnostic plot: `onrobot_tcpdaq_readonly/onrobot_tcpdaq_readonly_60s_20260523_194819_frequency_diagnostic.png`.

The OnRobot TCP request/response row rate is PC polling throughput, not the physical sensor update rate. The raw tuple change rate is not the same as physical sample rate either: in a static no-contact pose, quantized raw values can repeat even when the sensor has produced a new sample. Treat the `100.3 Hz` raw-change result as an observed distinct-value change rate for this static run, not as a definitive OnRobot sensor update specification.

| Field | Mean | Min | Max | Range | Std |
| --- | ---: | ---: | ---: | ---: | ---: |
| fx_n | 0.503 | 0.160 | 0.780 | 0.620 | 0.094 |
| fy_n | 3.480 | 3.280 | 3.640 | 0.360 | 0.050 |
| fz_n | -33.120 | -33.960 | -32.160 | 1.800 | 0.236 |
| tx_nm | -0.106 | -0.136 | -0.076 | 0.060 | 0.008 |
| ty_nm | -0.088 | -0.114 | -0.056 | 0.058 | 0.008 |
| tz_nm | 0.088 | 0.073 | 0.103 | 0.030 | 0.004 |

## Interpretation

- The switch topology is working for simultaneous UR10e and OnRobot access.
- UR10e Dashboard and RTDE were reachable and stable during the run.
- OnRobot TCP DAQ was reachable and stable during the run.
- UR `actual_TCP_force` and OnRobot wrench were collected in the same static no-contact posture, but their absolute values should not be expected to match because UR payload/TCP are still not calibrated for the mounted stack.
- For maximum frequency:
  - UR RTDE `actual_TCP_force`: use `500 Hz` as the practical read rate from this run.
  - OnRobot TCP DAQ row polling: about `5.67 kHz`, with a short-interval maximum around `6.78 kHz`.
  - OnRobot raw wrench distinct-value change rate in this static run: about `100 Hz`.
  - OnRobot URCap/PolyScope-facing HEX variables are documented/advertised at `125 Hz`; this is a different layer from direct TCP polling and from raw tuple distinct-value changes.

## Next Gate

Before any motion, confirm the EOAT and cables are mechanically clear at J4/J5/J6. If static testing continues, the next low-risk step is a longer 600 s no-contact run in the same posture.
