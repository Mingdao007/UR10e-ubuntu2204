# Handoff Prompt: UR10e / OnRobot HEX-E v2 Interface State

Paste this into the next Codex thread.

```text
继续当前 UR10e + OnRobot HEX-E v2 / Compute Box 数据接口排查。请先使用 `ur10e-realsetup` 和 `ur10e-onrobot-hex` skill，并先读：

- `/home/andy/ur10e_lab_vault/onrobot/hex_e_v2_3010007655/current_hardware_state.md`
- `/home/andy/codex-private-skills/skills/ur10e-onrobot-hex/references/frequency-and-interfaces.md`
- `/home/andy/codex-private-skills/skills/ur10e-onrobot-hex/SKILL.md`

当前硬件事实：

- UR10e: `192.168.1.18`
- Ubuntu bench Ethernet: `enp3s0 = 192.168.1.10/24`
- OnRobot Compute Box: `192.168.1.1`, observed web/PolyScope version `4.1.8`
- Installed URCap family: `ft-onrobot`, PolyScope shows `FT-OnRobot` version `4.1.7`
- Current topology: UR10e, Compute Box, and Ubuntu are reachable via small unmanaged Ethernet switch
- Safety boundary: no robot motion, no zero/bias, no TCP/payload writes, no URCap install/uninstall, no Compute Box setting changes, no vendor template programs unless I explicitly approve that exact action.

Important interface conclusions from the previous thread:

1. We do not have an exposed analog force-output port in the current setup. All usable routes are digital.
2. UR RTDE `actual_TCP_force` on `30004` is digital and has been measured at about `500 Hz`, but it is UR internal force readback, not raw OnRobot HEX output.
3. OnRobot TCP DAQ `192.168.1.1:49151` is the current safest PC-side route. It sends `READCALIBRATIONINFO` once and repeated `READFT`; it does not send zero/bias/filter/speed. Current post-URCap/toolbar-state measured raw tuple transition rate is about `251.5 Hz`, but TCP DAQ force values do not currently match PolyScope OnRobot Variables (`READFT` Fz about `-32 N` while PolyScope variable Fz was near zero). Treat this as a zero/reference/compensation mismatch.
4. OnRobot Web/Socket.IO is only about `9 Hz`.
5. PolyScope OnRobot variables (`Fx/Fy/Fz/Tx/Ty/Tz`, `tFT`) are visible and populated. The preferred route to get those exact near-zero variables into Ubuntu is manual no-motion PolyScope mapping to `output_double_register_24..29`, then Ubuntu RTDE read with `sample_urcap_ft_rtde_registers.py`. This still requires user-approved manual PolyScope program edits/running.
6. High-speed UDP exists on Compute Box / Ethernet DAQ port `49152/UDP`. Vendor file:
   `/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/vendor_usb_backups/onrobot_usb_20260524/Interfaces and Softwares/Ethernet/Example/highspeed_udp.c`
   The response is 36 bytes: `sequenceNumber`, `sampleCounter`, `status`, `Fx/Fy/Fz/Tx/Ty/Tz` as signed int32. In the sample, force is divided by `10000.0`, torque by `100000.0`. Datasheet lists max sampling frequency `500 Hz`.
7. Do not run vendor `highspeed_udp.c` as-is. It sends `COMMAND_SPEED`, `COMMAND_FILTER`, and toggles `COMMAND_BIAS` mid-run. A safer probe would need to be custom no-bias/no-filter/no-speed, and even that is not passive read-only because it still sends UDP stream start/stop commands. Do not run it live without explicit approval.
8. CBRCI / vendor robot-controller API:
   - Discovery: `55660/UDP`
   - Real-Time Interface: `32000/TCP`, one connection
   - Command Interface: `32002/TCP`, JSON ending `\r\n\r\n`
   - It is not a simple logger. It is for robot-controller integration: configuration, Robot Data, Robot Control speed replies, sensor values, bias, filter_mode, force/torque control, move, path recording.
   - Configuration is required before data transfer. `sensor_cycle` range is `2-254 ms`, so config-level minimum sensor interval is `2 ms`, not `1 ms`.
   - Manual recommends Robot Data no faster than every `4 ms`; if another Robot Data message arrives before the prior Robot Control reply, messages are buffered and replies are sent in order.
   - Therefore do not say "1 ms polling is fine but less accurate"; likely failure mode is queueing/stale replies/latency/jitter, not clean 1 kHz force data.
   - CBRCI should not be first choice for high-frequency logging.

Recommended next options:

- If the goal is safest useful data now: continue `49151/TCP READFT`, but keep force-value mismatch caveat.
- If the goal is the PolyScope near-zero OnRobot variable口径: plan the manual no-motion RTDE output-register mapping.
- If the goal is verifying datasheet-level `500 Hz`: implement a custom high-speed UDP parser/probe script, but stop before live execution and ask for explicit approval because even the safer probe sends stream start/stop.
- Avoid CBRCI unless the goal becomes full robot-controller integration.

Please keep responses concise and in Chinese by default. Do not command robot motion or write robot/Compute Box settings without an explicit approval step.
```
