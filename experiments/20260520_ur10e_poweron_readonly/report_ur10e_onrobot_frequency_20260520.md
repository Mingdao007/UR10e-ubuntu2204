# UR10e / OnRobot frequency check report

Date: 2026-05-20  
Bench: UR10e + OnRobot HEX-E v2 / Compute Box  
Operator goal: record UR10e internal force read frequency and clarify the
maximum frequency expectation for OnRobot.

## Summary

- UR10e RTDE `actual_TCP_force` was tested for 60 s with requested `500 Hz`.
  The run succeeded and recorded `30001` samples over `60.001167 s`, giving an
  effective rate of `500.0069 Hz`.
- OnRobot HEX-E through the current Compute Box Web / Socket.IO stream is much
  slower. The fastest local read test so far used `--poll-interval-s 0.005` and
  still recorded only `137` rows over `14.971020 s`, i.e. `9.1510 Hz`.
- These two facts do not contradict the OnRobot datasheet-level claim. The
  older official OnRobot HEX-E Sensor 2.0 datasheet states a maximum sampling
  frequency of `500 Hz` and lists interfaces including USB, CAN, Ethernet
  TCP/UDP, and EtherCAT. Our current logger is not using that low-level
  high-rate interface; it is reading the web client Socket.IO polling stream.

## UR10e RTDE 500 Hz Test

Command:

```bash
python3 scripts/sample_tcp_force.py \
  --seconds 60 \
  --hz 500 \
  --output-dir /home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/ur_rtde_maxfreq_60s_20260520_154446 \
  --prefix UR10e_actual_TCP_force_500hz_60s \
  --plot all \
  --json-only
```

Preflight state:

- Ubuntu direct link: `enp3s0 = 192.168.1.10/24`
- UR10e: `192.168.1.18`
- Dashboard safety mode: `NORMAL`
- Robot mode: `RUNNING`
- Program state: `STOPPED <unnamed>`
- No robot motion was commanded by the sampling script.

Artifacts:

- CSV:
  `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/ur_rtde_maxfreq_60s_20260520_154446/UR10e_actual_TCP_force_500hz_60s_20260520_154546.csv`
- Full force plot:
  `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/ur_rtde_maxfreq_60s_20260520_154446/UR10e_actual_TCP_force_500hz_60s_20260520_154546.png`
- Fz plot:
  `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/ur_rtde_maxfreq_60s_20260520_154446/UR10e_actual_TCP_force_500hz_60s_20260520_154546_fz.png`
- Fx/Fy plot:
  `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/ur_rtde_maxfreq_60s_20260520_154446/UR10e_actual_TCP_force_500hz_60s_20260520_154546_fxy.png`

Measured timing:

| Metric | Value |
|---|---:|
| Requested RTDE frequency | `500 Hz` |
| Samples | `30001` |
| First timestamp | `0.000273943 s` |
| Last timestamp | `60.001441002 s` |
| Last-first duration | `60.001167059 s` |
| Rows / duration | `500.006941 Hz` |
| 1 / mean dt | `499.990275 Hz` |
| Mean dt | `2.000039 ms` |
| Median dt | `2.000093 ms` |
| p95 dt | `2.026081 ms` |
| p99 dt | `2.053022 ms` |
| Min dt | `0.013113 ms` |
| Max dt | `5.626917 ms` |

Interpretation:

- UR10e RTDE can deliver the internal force channel at `500 Hz` in this setup.
- The previous `20 Hz` runs were limited by our selected script argument, not
  by the UR10e interface.
- This result is suitable for high-rate logging of UR internal
  `actual_TCP_force`. It does not by itself validate force accuracy, payload,
  TCP, contact safety, or frame alignment.

## OnRobot Web Stream Tests

Current local read path:

```text
Compute Box /version + Socket.IO polling stream
```

Representative runs:

| Run | Rows | Duration | Effective rate |
|---|---:|---:|---:|
| Direct link quick check, default polling | `91` | `9.906348 s` | `9.1860 Hz` |
| Direct link fast polling, `--poll-interval-s 0.005` | `137` | `14.971020 s` | `9.1510 Hz` |
| Mounted EOAT 60 s segment | `543` | `59.997907 s` | `9.0503 Hz` |
| Mounted EOAT 600 s segment | `5394` | `600.015913 s` | `8.9898 Hz` |
| Mounted EOAT 1800 s segment | `16168` | `1799.909038 s` | `8.9827 Hz` |

Fast-poll artifact:

- `/home/andy/ur10e_ros2_ws/ft_sensor/onrobot/hex_e_v2_3010007655/measurements/direct_onrobot_fastpoll_check_20260520_154155/summary.json`

Interpretation:

- The current Socket.IO polling stream is effectively about `9 Hz`.
- Reducing the client polling interval from `0.05 s` to `0.005 s` did not raise
  the data rate, so this is not just a slow Python sleep setting.
- This web-stream path is acceptable for drift, static, and quasi-static
  monitoring. It is not appropriate as a high-rate force feedback source.

## OnRobot Maximum Frequency Interpretation

Evidence:

- OnRobot current B2B product page identifies the HEX-E QC as a 6-axis force
  torque sensor and links its official datasheet:
  `https://b2b.onrobot.com/hex-e-qc/`
- The current datasheet link is:
  `https://onrobot.com/storage/datasheets/datasheet_hex.pdf`
- Search-indexed text for the older official OnRobot HEX-E Sensor 2.0
  datasheet at
  `https://onrobot.com/sites/default/files/documents/en_hex-e_datasheet_october_2018.pdf`
  states that the interface types include USB, CAN, Ethernet TCP/UDP, and
  EtherCAT, and that the maximum sampling frequency is `500 Hz`.

Conclusion:

- Sensor/interface capability to investigate: up to `500 Hz`.
- Confirmed with our current logger: only about `9 Hz` over the Compute Box
  Web / Socket.IO stream.
- The gap means we need a different OnRobot access path for high-rate data:
  official low-level Ethernet TCP/UDP, USB/CAN, EtherCAT, URCap-exposed
  variables, or vendor SDK if available. The Web Client stream should not be
  treated as the high-rate API.

## Next Steps

1. Keep using UR10e RTDE `actual_TCP_force` at `500 Hz` for high-rate internal
   force logging.
2. Use OnRobot Socket.IO only for low-frequency external reference until the
   high-rate official interface is identified and tested.
3. Search the OnRobot USB drive / package for SDK files, protocol manuals, or
   examples for Ethernet TCP/UDP, USB/CAN, EtherCAT, or ROS.
4. If the high-rate API cannot be recovered from the package, contact OnRobot
   or the lending lab for the HEX-E v2 low-level interface documentation.
5. Once the high-rate OnRobot interface is found, run the same frequency test:
   60 s no-motion/no-contact capture, rows/duration, dt p95/p99, and error
   count.

## Official OnRobot / UR Integration Findings

Follow-up document search on 2026-05-20 found the official UR-side integration
path that matches the remembered "OnRobot and UR collaboration" material.

Primary official sources:

- OnRobot Learn, `Universal Robots e-Series URCap & understanding`:
  `https://learn.onrobot.com/en/universal-robots-e-series-urcap-understanding?area=51`
- OnRobot Learn, `HEX getting started`:
  `https://learn.onrobot.com/en/hex-getting-started?area=56`
- OnRobot Learn, `HEX QC getting started`:
  `https://learn.onrobot.com/en/hex-qc-getting-started?area=56`
- Universal Robots Marketplace, `HEX Force/Torque Sensing Package`:
  `https://www.universal-robots.com/plus/products/onrobot/hex-forcetorque-sensing-package/`

Relevant facts from those sources:

- OnRobot's HEX package includes a tool data cable from HEX to Compute Box, an
  Ethernet cable between the Compute Box and the robot, and a USB stick with
  product files/information.
- The OnRobot URCap setup page discovers Compute Boxes on the same network as
  the robot, shows component IP addresses, serial numbers, and firmware
  versions, and can offset RTDE registers when multiple URCaps are used.
- The URCap page notes that TCPs are read from the tools and can also be
  modified inside the URCap setup.
- The Universal Robots Marketplace page states that, inside UR PolyScope, the
  HEX force/torque signal `Fx,Fy,Fz,Tx,Ty,Tz` is available via provided program
  templates and is updated at `125 Hz`.
- The same Marketplace page lists UR10e compatibility for the HEX
  Force/Torque Sensing Package.

Interpretation:

- The official OnRobot + UR route appears to be a URCap / PolyScope program
  template route, not the Compute Box Web Client route tested by our current
  Python Socket.IO logger.
- This route is likely the right next practical path if `~125 Hz` external HEX
  data is enough for logging or moderate-rate force features.
- It does not prove that a PC-side Python program can read the OnRobot HEX at
  `500 Hz`. The `500 Hz` number still points to lower-level interfaces
  documented by older OnRobot sensor/Compute Box material, such as USB, CAN,
  Ethernet TCP/UDP, or EtherCAT. The direct official URL previously used for
  the Compute Box robot-controller interface PDF now redirects to the OnRobot
  home page, so the low-level protocol remains unresolved in this workspace.

Safety / execution boundary:

- Do not install or update OnRobot URCap yet.
- Do not run OnRobot program templates yet.
- Do not write UR TCP/payload or OnRobot configuration yet.
- Safe next step is only to inspect the OnRobot USB stick contents or obtain
  the matching OnRobot UR manual / Compute Box interface PDF, then decide
  whether to test URCap discovery in a no-motion state.

## Local USB / Manual Intake

The OnRobot USB stick and the Mac-transferred `manual4UR.pdf` were inspected
read-only on 2026-05-20. Full intake notes are in:

`/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/onrobot_usb_manual_intake_20260520.md`

Relevant additions:

- `manual4UR.pdf` is an older OnRobot HEX UR manual for URCap Plugin `3.1.3`
  and explicitly states that the URCap feedback variables are updated at
  approximately `125 Hz`.
- The USB contains `FT-OnRobot-4.1.7.1754.urcap`, whose manifest reports
  `Bundle-Version: 4.1.7`, plus a `Compute_Box_SW_update_v4.1.8.osu` update
  package. The update package was only identified, not run.
- The USB contains `User Manual for the UR HEX Sensor Kit_E14_en.pdf`, which
  targets URCap Version `4.1.4`. It says the default Compute Box IP is
  `192.168.1.1`, the UR robot should be on `192.168.1.x/24` for factory
  Compute Box settings, and the Compute Box software version should match the
  URCap version.
- The USB contains the official `OnRobot Compute Box Robot Controller Interface
  Description_E7.pdf`. It documents UDP discovery on `55660`, a Real-Time TCP
  interface on `32000`, a Command TCP interface on `32002`, and a recommended
  robot-controller message interval no faster than `4 ms`.
- The USB also contains standalone Ethernet DAQ examples: TCP on `49151` and
  high-speed UDP on `49152`. The local HEX-E v2 datasheet on the same USB
  states `Maximum sampling frequency 500 Hz` and lists `Ethernet TCP/UDP`.
- The high-speed UDP example must not be run as-is because its sample flow sets
  speed/filter and toggles bias. A modified no-bias logger is required before
  any live high-rate UDP test.

Current cable-state note from read-only Ubuntu checks:

- `enp3s0` is configured as `192.168.1.10/24`.
- `192.168.1.1` is reachable and has TCP ports `80`, `32000`, `32002`, and
  `49151` open.
- `192.168.1.18` is not reachable in the current wiring state.
- No USB Ethernet adapter was visible in `lsusb`; the active Ethernet interface
  is PCI `enp3s0` using driver `r8169`.

Interpretation:

- A USB-C-to-Ethernet adapter can work in principle, but it must appear as a
  network interface and be assigned a compatible static IP.
- In the observed wiring state, the PC is connected to the OnRobot Compute Box
  side, not to the UR10e side.
- For URCap discovery, UR10e and Compute Box must be mutually visible on the
  same `192.168.1.x/24` subnet. A single PC-to-one-device Ethernet link does
  not automatically bridge UR10e and Compute Box.

## OnRobot TCP DAQ Read-Only 60 s Test

After the USB intake, a workspace-local read-only TCP logger was created from
the vendor `tcp.c` protocol:

`/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/scripts/sample_onrobot_tcp_daq.py`

Safety boundary:

- The script sends `READCALIBRATIONINFO = 1` once before sampling.
- The script sends only `READFT = 0` during sampling.
- It does not send bias, filter, speed, zero, TCP, payload, URScript, URCap, or
  robot-motion commands.

Command:

```bash
python3 /home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/scripts/sample_onrobot_tcp_daq.py \
  --seconds 60 \
  --output-dir /home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/onrobot_tcpdaq_readonly_60s_20260520 \
  --prefix onrobot_tcpdaq_readonly_maxpoll_60s
```

Artifacts:

- CSV:
  `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/onrobot_tcpdaq_readonly_60s_20260520/onrobot_tcpdaq_readonly_maxpoll_60s_20260520_161711.csv`
- Summary:
  `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/onrobot_tcpdaq_readonly_60s_20260520/onrobot_tcpdaq_readonly_maxpoll_60s_20260520_161711_summary.json`
- Repeated raw tuple analysis:
  `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/onrobot_tcpdaq_readonly_60s_20260520/onrobot_tcpdaq_readonly_maxpoll_60s_20260520_161711_raw_tuple_change_analysis.json`

Calibration returned by the Compute Box:

| Field | Value |
|---|---:|
| `counts_per_force` | `10000` |
| `counts_per_torque` | `100000` |
| `force_units` | `2` |
| `torque_units` | `3` |
| `scale_factors` | `[200, 200, 200, 100, 100, 65]` |

TCP request-response timing:

| Metric | Value |
|---|---:|
| Samples | `348853` |
| First-last duration | `59.997908226 s` |
| Rows / duration | `5814.419374 Hz` |
| 1 / mean dt | `5814.402707 Hz` |
| Mean dt | `0.171987 ms` |
| Median dt | `0.164171 ms` |
| p95 dt | `0.198402 ms` |
| p99 dt | `0.258070 ms` |
| Max dt | `3.587605 ms` |
| Errors | `0` |

Repeated raw tuple analysis:

| Metric | Value |
|---|---:|
| Unique raw six-axis tuples | `6023` |
| Consecutive raw tuple changes | `6036` |
| Consecutive raw tuple change rate | `100.603507 Hz` |
| Mean reads per constant raw tuple run | `57.785821` |
| Max reads per constant raw tuple run | `65` |

Interpretation:

- The TCP read path can be polled very quickly from the PC: about `5.8 kHz`
  request-response rows in this run.
- That is not the same as the physical sensor sample/update rate. This TCP
  response format has no sample counter, and the returned raw six-axis tuple was
  repeated many times.
- In this static run, the returned raw tuple changed at about `100.6 Hz`. That
  is the best current estimate of the new-value rate for the default read-only
  TCP DAQ path.
- Therefore the safe read-only conclusion is:
  - Web / Socket.IO path: about `9 Hz`.
  - TCP DAQ passive read path: about `5.8 kHz` poll throughput, but only about
    `100 Hz` observed new raw tuples in the current default state.
  - URCap / PolyScope feedback variables: documented around `125 Hz`.
  - The datasheet `500 Hz` capability is still not confirmed by this read-only
    TCP test; it likely requires the high-speed UDP path and/or speed/filter
    configuration, which is not a passive read-only operation.
