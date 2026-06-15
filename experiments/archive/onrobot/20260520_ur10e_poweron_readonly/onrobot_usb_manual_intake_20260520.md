# OnRobot USB and manual4UR intake

Date: 2026-05-20  
Scope: read-only inspection of the Mac-transferred `manual4UR.pdf` and the mounted OnRobot USB drive.  
Safety boundary: no URCap installation, no Compute Box update, no UR program execution, no robot motion, no F/T zero, no TCP/payload write.

## Sources

### Mac-transferred manual

- Path: `/home/andy/ur10e_ros2_ws/From_Mac/20260520-160603_manual4UR/manual4UR.pdf`
- SHA-256: `5b9f0c49ce45d097e540a8898f82b7538b3f57c0cb492d460a90ef345c8f649a`
- PDF metadata: 68 pages, created 2018-06-14.
- Title text: `HEX Force Torque Sensor`, `For the Universal Robots`, `OnRobot FT URCap Plugin Version 3.1.3`, Edition E10.

Key evidence:

- The manual describes the OnRobot URCap feedback variables `F3D`, `Fx`, `Fy`, `Fz`, `T3D`, `Tx`, `Ty`, `Tz`, `bFT`, `of_compute_engine_ping`, `of_compute_engine_ping_max`, and `tFT`.
- It explicitly states those variables are updated automatically at approximately `125 Hz`.
- It documents `F/T Zero`; this biases the sensor and must not be used while the tool is touching anything.
- It documents TCP offset propagation: after changing TCP offset inside a program, `of_send_tcp_offset()` must be run to propagate the change to the Compute Box.

Interpretation:

- `manual4UR.pdf` is useful because it confirms the remembered URCap-side update rate of about `125 Hz`.
- It is older than the USB E14 manual and older than the URCap found on the USB stick, so it should be used as historical evidence, not as the exact install manual for the current package.

### OnRobot USB stick

- Block device: USB drive label `ONROBOT`, mounted at `/media/andy/ONROBOT`.
- Mount evidence: `/media/andy/ONROBOT` is mounted from `sda1`, FAT volume UUID `2C2B-A55A`.

Important files:

| Item | Path | SHA-256 / version | Interpretation |
|---|---|---|---|
| URCap | `/media/andy/ONROBOT/OnRobot_UR_Programs/FT-OnRobot-4.1.7.1754.urcap` | `5ec764364b8fbf7cc8a21dec8e7ec59636e96501fbf95fb5b3cfb03d33182908`; manifest `Bundle-Version: 4.1.7` | OnRobot URCap package for Force Torque Sensors. Contains RTDE-related Java classes and many URScript resources. |
| Compute Box update | `/media/andy/ONROBOT/Compute_Box_update/Compute_Box_SW_update_v4.1.8.osu` | `998c3e0f81187afed77b561cea19b36bcf43dae4f732947a50ec8dc7e0345856` | Firmware/software update package. Do not run unless explicitly approved after compatibility check. |
| UR HEX manual E14 | `/media/andy/ONROBOT/HEX User Manual for UR/E14/User Manual for the UR HEX Sensor Kit_E14_en.pdf` | `c474f07b9c6f3a746b14ce49d19b8fed89f49d1473c88387ef7da265dcce5e12`; text says `For URCap Version 4.1.4`, 76 pages, created 2019-01-22 | Main UR kit manual on the USB. Newer than `manual4UR.pdf` but still not exactly the `4.1.7` URCap version. |
| Compute Box Robot Controller Interface manual | `/media/andy/ONROBOT/Interfaces and Softwares/Ethernet/Manual/OnRobot Compute Box Robot Controller Interface Description_E7.pdf` | `5e87ac05c427acfe818aaa75a07dda8ae40cc4b6c985aabbada0386b5a4d0fd6`; 39 pages, created 2018-07-18 | Official low-level Compute Box interface manual, useful for PC-side or robot-controller integration analysis. |
| HEX-E v2 datasheet | `/media/andy/ONROBOT/Datasheet/HEX-E/v2/EN_HEX-E_Datasheet_October_2018.pdf` | `69fe8f8b89d44cfa7f78ba0023077714cd73d9980c5481bb90486ec49a997c59`; 3 pages, created 2018-10-17 | Lists `Ethernet TCP/UDP`, `EtherCAT`, USB/CAN interfaces and `Maximum sampling frequency 500 Hz`. |
| TCP C example | `/media/andy/ONROBOT/Interfaces and Softwares/Ethernet/Example/tcp.c` | TCP port `49151` | Simple Ethernet DAQ read example. It sends read requests and receives force/torque values; this is the safest candidate for a first PC-side no-motion test. |
| High-speed UDP C example | `/media/andy/ONROBOT/Interfaces and Softwares/Ethernet/Example/highspeed_udp.c` | UDP port `49152` | High-speed DAQ example. It sets speed/filter and toggles bias in the sample flow, so it must not be run as-is. |
| USB auto script | `/media/andy/ONROBOT/urmagic_onrobot.sh` | copies config and runs Java on UR | Intended for UR-side USB behavior. Do not execute on Ubuntu or manually on the robot. |

## URCap / PolyScope path

The USB manual and URCap package support the official OnRobot + UR route:

- The URCap setup discovers OnRobot devices on the same network as the UR robot.
- The E14 manual states the default Compute Box IP is `192.168.1.1`.
- It states the Compute Box software version must match the URCap version; if not, the manual says to update the Compute Box.
- It states the UR robot IP must be in `192.168.1.x` with subnet mask `255.255.255.0` when using factory Compute Box settings.
- It includes TCP configuration options and recommends setting TCP from the sensor flange in the URCap workflow.
- It documents `F/T Zero` and `F/T Set TCP`; these are write/bias actions and must not be used during read-only diagnostics.

The `.urcap` archive itself contains:

- `Bundle-Name: FT-OnRobot`
- `Bundle-Version: 4.1.7`
- RTDE communication classes under `com/onrobot/libraries/comm/rtde/`
- URScript resources including `C03_BIAS`, `C04_TCP`, `C06_PAYLOAD`, force-control, hand-guide, move, waypoint, insert, and path scripts.

Interpretation:

- URCap is probably the correct route for official UR-side OnRobot use and about `125 Hz` program-variable access.
- It is not a passive PC-side 500 Hz logger.
- Installing URCap or running `.urp` examples is outside the current read-only/no-motion boundary.

## Low-level Ethernet path

The Compute Box Robot Controller Interface manual documents a lower-level Ethernet route:

- Discovery: UDP broadcast port `55660`.
- Real-Time Interface: TCP port `32000`, one TCP connection at a time.
- Command Interface: TCP port `32002`, JSON messages terminated by `\r\n\r\n`, multiple clients allowed.
- Robot Data message length: 76 bytes.
- Robot Control response length: 58 bytes, including six force/torque sensor values in `N` and `Nm`.
- The manual recommends not sending Robot Data messages more often than every `4 ms`, i.e. roughly `250 Hz` maximum for this robot-controller loop.
- Configuration includes `robot_cycle` and `sensor_cycle`; `sensor_cycle` is the interval at which the sensor sends data.
- Filter mode value `1` corresponds to `500 Hz`.

Interpretation:

- This interface is not the same as the web Socket.IO stream that measured about `9 Hz`.
- It is also not purely passive: the documented workflow sends configuration and may send robot state / command messages.
- It may be useful later, but the first PC-side DAQ test should use the simpler Ethernet DAQ example before trying CBRCI.

## Standalone Ethernet DAQ path

The USB includes direct Ethernet DAQ examples independent of URCap:

- `tcp.c` uses TCP port `49151`.
- `highspeed_udp.c` uses UDP port `49152`.
- `highspeed_udp.c` defines `SPEED 10`, with the comment `1000 / SPEED = Speed in Hz`.
- `highspeed_udp.c` defines filter modes including `1 = 500 Hz`.
- The sample calls speed, filter, and bias commands in `main()`, including a later bias toggle.

Interpretation:

- This is the strongest local evidence that the 500 Hz claim belongs to the sensor/DAQ Ethernet interface, not the web UI stream.
- The UDP sample must be edited before any live run so it never sends bias and does not change filter/speed unless explicitly approved.
- The TCP sample is the lower-risk first candidate because it appears to read calibration and force/torque values without zeroing the sensor.

## Current network evidence

Read-only Ubuntu checks at the time of intake:

- `enp3s0` is up with `192.168.1.10/24`.
- Default route stays on Wi-Fi; the UR/OnRobot Ethernet link has no gateway.
- `192.168.1.1` responds to ping and has TCP ports `80`, `32000`, `32002`, and `49151` reachable.
- `192.168.1.18` does not respond to ping, Dashboard `29999`, or RTDE `30004` in the current cable state.
- `ip neigh` shows `192.168.1.1` reachable with MAC `b8:27:eb:a1:40:b7`, and `192.168.1.18` failed.
- `lsusb` currently shows the OnRobot USB disk but no USB Ethernet adapter. The active Ethernet interface is PCI `enp3s0` using driver `r8169`.

Interpretation:

- A USB-C-to-Ethernet adapter can work in principle; Linux just treats it as another NIC.
- In the current observed state, the computer is seeing the OnRobot Compute Box at `192.168.1.1`, not the UR10e at `192.168.1.18`.
- If the goal is URCap discovery, UR10e and Compute Box must be on the same `192.168.1.x/24` network. A single PC Ethernet connection to only one device does not automatically make the PC, UR10e, and Compute Box all mutually visible.
- If using a USB-C adapter, it should appear as a separate network interface, often named `enx...` or similar. No such interface was visible during this check.

## Safe next steps

1. Do not install the URCap, run `.urp` examples, update the Compute Box, execute `F/T Zero`, or execute `F/T Set TCP` yet.
2. For the UR10e link question, physically decide which target the PC is connected to:
   - PC to UR10e: expect `192.168.1.18` reachable.
   - PC to Compute Box: expect `192.168.1.1` reachable.
   - URCap route: UR10e and Compute Box need to share the same subnet, usually through the UR control box/network path or a small switch depending on the bench wiring.
3. For PC-side OnRobot high-rate investigation, first create a workspace-local copy of `tcp.c`, strip it to a 60 s no-motion read-only logger, and measure rows/duration against `192.168.1.1:49151`.
4. Only after the TCP read path is understood, consider a modified UDP logger from `highspeed_udp.c`. The live version must remove bias toggling and avoid changing filter/speed unless explicitly approved.
5. If the URCap route is chosen, do it as a separate no-motion procedure: backup UR installation, confirm matching versions, install URCap, check discovery only, and do not run templates or write TCP/payload.
