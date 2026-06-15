# Goal Prompt: Borrowed UR10e Robotiq FT 300-S 8h Drift

Copy everything below into `/goal` on the Ubuntu / robot logging agent.

```md
/goal

你现在在 Ubuntu / robot logging agent 上执行一个只读实验准备任务。目标是给“另一台借用 UR10e 上的 Robotiq FT 300-S”做 8 小时 drift / thermal drift characterization。注意：这不是 Mingdao 自己的 UR10e，也不是 OnRobot HEX-E。

Hard labels:
- robot_identity: borrowed_ur10e_not_mingdao_main_ur10e
- sensor_model: Robotiq FT 300-S
- experiment_type: static_no_contact_8h_drift
- do_not_mix_with: OnRobot HEX-E 3010007655, Mingdao main UR10e zero drift

Create experiment root:
`/home/andy/ur10e_ros2_ws/ft_sensor/robotiq/borrowed_ur10e_ft300s_$(date +%Y%m%d_%H%M%S)/`

First, do only read-only checks:
1. Ask/operator-confirm the borrowed UR10e robot IP if it is not already known.
2. Record `ip addr`, routing, and robot reachability.
3. Check ports `29999 30001 30002 30003 30004`.
4. Do not move the robot.
5. Do not run calibration.
6. Do not call Zero sensor yet.
7. Do not change URCap, firmware, safety, payload, TCP, or installation settings.

Reading path:
- Preferred: use UR controller + Robotiq URCap script functions:
  `get_sensor_Fx()`, `get_sensor_Fy()`, `get_sensor_Fz()`,
  `get_sensor_Mx()`, `get_sensor_My()`, `get_sensor_Mz()`.
- If these functions are undefined, stop and report that Robotiq URCap FT300-S reading path is unavailable.
- Do not substitute UR internal `actual_TCP_force` as FT300-S data. It may be logged only as a clearly labelled comparison channel.

Dry run:
- Run a 2-minute no-motion, no-contact read-only logger.
- Save CSV and events.
- Pass criteria:
  - CSV grows continuously;
  - 6-axis values are numeric;
  - no robot motion command is sent;
  - no zero/calibration is called;
  - no protective stop or ownership conflict.

Calibration / zero policy:
- Do not run full calibration unless the hardware owner explicitly approves.
- If owner approves temporary zero, first save 10-15 minutes no-zero baseline, then call one temporary `Zero sensor` and mark `zero_sensor_called=true`.
- If no approval, run the full 8h as no-zero raw drift.

8h run:
- Static pose, no contact, no motion.
- Log for 8 hours.
- Flush at least every 60 seconds.
- Every 30 minutes print status summary.
- Stop and mark invalid if someone touches the tool, robot moves unexpectedly, another program starts, communication is lost for >5 minutes, or a protective stop occurs.

Outputs:
- `raw/ft300s_drift.csv`
- `events.jsonl`
- `summary.json`
- `analysis/drift_plot.png`
- `analysis/drift_bins_10min.csv`
- `report.md`

The report must start with:
“This run is from a borrowed/other UR10e with Robotiq FT 300-S. It is not Mingdao’s main UR10e, not OnRobot HEX-E, and must not be merged into those baselines.”

End by reporting exact output paths and whether the run is valid.
```

## Source Notes

- ✅ confirmed: Robotiq FT 300-S product page says FT 300-S has ±300 N range and is designed for repeatable force/torque sensing, with Force Copilot integration context: [Robotiq FT 300-S](https://robotiq.com/products/ft-300-force-torque-sensor)
- ✅ confirmed: Robotiq notes `Zero sensor` is a temporary offset and should be used before tasks needing repeatable relative readings: [Operation and Calibration of the FT-300S](https://robotiq.zendesk.com/hc/en-us/articles/9929019993619-Operation-and-Calibration-of-the-FT-300S)
- ✅ confirmed: Robotiq warns FT300-S is not a precision weighing/measurement instrument and can drift over time/environment: [General Measurements](https://robotiq.zendesk.com/hc/en-us/articles/4411820804115-Robotiq-FT-300-Sensor-FT-300-S-Sensor-and-Copilot-Software-General-Measurements)
- ✅ confirmed: Robotiq FT300-S measurement specificities mention short-period relative measurement and possible 10-20 N offset after 5-10 min: [FT300S measurement specificities](https://robotiq.zendesk.com/hc/en-us/articles/1500010482762-FT300S-measurement-specificities)
- ✅ confirmed: manual documents URCap functions including `get_sensor_Fx/Fy/Fz/Mx/My/Mz` and recommends calibration after install/reinstall: [FT 300 Instruction Manual PDF](https://assets.robotiq.com/website-assets/support_documents/document/FT_Sensor_Instruction_Manual_PDF_20190227.pdf)

