# Mounted EOAT Continuous-power Rest 60/600/3600 Preflight Notes

- Run root: `/home/andy/ur10e_ros2_ws/ft_sensor/onrobot/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_3600_20260520_112124`
- Experiment root: `/home/andy/ur10e_ros2_ws/ft_sensor/onrobot/hex_e_v2_3010007655`
- Branch: `exp/onrobot-mounted-eoat-rest-60-600-3600-20260520`
- User instruction: self-designed EOAT has been mounted; UR10e is not powered on; rerun 60 s, 600 s, and 3600 s tests; rest between tests for cooling/recovery.
- Agreed protocol: continuous-power rest/recovery, not true power-cycle cooling.
- Reason: user will leave; no remote/programmable Compute Box power switch is available in this workspace.
- Segment order: `S00_60s`, `R00_rest_30min`, `S01_600s`, `R01_rest_30min`, `S02_3600s`.
- Rest duration: 1800 s after `S00_60s` and after `S01_600s`.
- Hardware state: OnRobot HEX-E mounted on UR10e with the self-designed EOAT installed.
- UR10e state: not powered on; no UR interface reads, no dashboard, no RTDE, no ROS driver.
- Compute Box host: `192.168.1.1`.
- Ubuntu interface: `enp3s0`, `192.168.1.10/24`.
- Preflight ping gate: 4/4 replies, 0% packet loss.
- Compute Box version: `{"version":"4.1.8"}`.
- Safety/read-only policy: no UR motion, no UR `zero_ftsensor()`, no OnRobot zero, no bias, no autocalib, no firmware, no DIP switch change, no config write, no TCP/payload/gravity/ROS/UR config changes.
- Contact state: user reported EOAT mounted; no intentional contact test is part of this protocol. Report must not infer contact-free force compensation without onsite confirmation.
