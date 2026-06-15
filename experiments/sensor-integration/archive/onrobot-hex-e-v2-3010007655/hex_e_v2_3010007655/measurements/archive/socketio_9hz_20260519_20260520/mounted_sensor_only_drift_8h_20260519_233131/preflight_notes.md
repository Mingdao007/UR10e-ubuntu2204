# Mounted Sensor-only 8 h Drift Preflight Notes

- Run root: `/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_sensor_only_drift_8h_20260519_233131`
- Goal prompt: `GOAL_RUN_ONROBOT_8H_MOUNTED_SENSOR_ONLY.md`
- Handoff: `HANDOFF_READY_FOR_DRIFT_20260519.md`
- User instruction: "已经插电 开始测吧 记得每一个小时都有checkpoint 最后写报告的时候可以用"
- Hardware state: OnRobot HEX-E mounted sensor-only on UR10e, according to Mac handoff.
- Compute Box power: user confirmed plugged in; use 24 V adapter only.
- Compute Box version: `{"version":"4.1.8"}`
- Ubuntu interface: `enp3s0`, `192.168.1.10/24`
- Compute Box host: `192.168.1.1`
- Ping gate: 4/4 replies, 0% packet loss.
- `P_work_drift`: not provided before run start; record later if user supplies it.
- No-contact/cable state: user requested start after mounting; Mac handoff says visual check suggests enough wrist/arm slack. No robot motion will be commanded by this run.
- Read-only policy: no zero, no bias, no autocalib, no firmware, no DIP switch change, no config write, no UR `zero_ftsensor()`, no robot motion.
