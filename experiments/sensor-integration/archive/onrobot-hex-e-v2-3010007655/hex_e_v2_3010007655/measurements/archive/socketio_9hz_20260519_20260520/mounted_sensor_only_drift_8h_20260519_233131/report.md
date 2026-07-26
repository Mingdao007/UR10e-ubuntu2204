# OnRobot HEX-E 安装在 UR10e 后 8 小时只读漂移记录

## 实验目的

本次实验记录 OnRobot HEX-E 安装在 UR10e 腕部后的 sensor-only 长时间漂移。目标不是验证接触控制，也不是做清零前后对比，而是在机器人静止、末端无接触、传感器已上电的条件下观察 8 小时只读力/力矩输出是否稳定。

本报告只比较同一条 8 小时主测内的 hourly checkpoints。所有 checkpoint 都由同一个 `main_8h/raw_wrench.csv` 按 `t_s <= N*3600` 截取，并用同一个分析脚本重算；因此表格口径一致。

## 设备与实验条件

| 项目 | 内容 |
|---|---|
| 传感器 | OnRobot HEX-E v2，live serial: `HEXEB806` |
| 文档/包名标识 | `3010007655` |
| 安装状态 | HEX-E 已安装在 UR10e 腕部；本 run 视为 sensor-only，无外接负载、无接触 |
| 机器人状态 | UR10e 静止；本实验不发送运动命令 |
| `P_work_drift` | 未在开跑前提供；报告不反推位姿 |
| Compute Box | `192.168.1.1`，`/version` 返回 `4.1.8` |
| Ubuntu 网口 | `enp3s0`，`192.168.1.10/24` |
| 运行目录 | `/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_sensor_only_drift_8h_20260519_233131` |
| 主测时长 | 28800 s，约 8 h |
| checkpoint | 01h 到 08h，每小时一个截断重分析目录 |
| 清零/偏置 | 未调用 UR `zero_ftsensor()`；未调用 OnRobot zero/bias/autocalib |
| 安全边界 | 只读采集；未调用 firmware、DIP switch、配置写入、TCP/payload/gravity/ROS/UR 配置修改 |
| fxy 规则 | Fx 和 Fy 两条曲线放在同一张图 |

预检记录见 [preflight_notes.md](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_sensor_only_drift_8h_20260519_233131/preflight_notes.md)。用户确认 Compute Box 已插电后开始；本机预检显示 `ping 192.168.1.1` 为 4/4 replies、0% packet loss。

## 实验命令

2 分钟 dry run：

```bash
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s 120 \
  --out-dir "/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_sensor_only_drift_8h_20260519_233131/dry_run_2min" \
  --status-every-s 30 \
  --flush-every-s 30
```

8 小时主测：

```bash
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s 28800 \
  --out-dir "/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_sensor_only_drift_8h_20260519_233131/main_8h" \
  --status-every-s 1800 \
  --flush-every-s 30
```

每小时 checkpoint 由 `make_hourly_checkpoints.py` 从正在增长的主 CSV 中截取 `t_s <= 3600, 7200, ..., 28800`，再运行：

```bash
python3 tools/analyze_onrobot_drift.py "$CHECKPOINT_DIR" --baseline-s 600 --bin-s 600
```

## 数据与图片

主测完整 force/torque 图如下。上图为 Fx/Fy/Fz，下图为 Tx/Ty/Tz；横轴是采集开始后的秒数。

![8 h OnRobot HEX-E force/torque](main_8h/force_torque.png)

Fz 单独图用于直接观察竖直方向读数在 8 小时内的变化。

![8 h OnRobot HEX-E Fz](main_8h/fz.png)

fxy 图把 Fx 和 Fy 放在同一坐标系中，用于判断水平分量是否同步漂移或各自偏移。

![8 h OnRobot HEX-E Fx/Fy](main_8h/fxy.png)

## 统计结果

Dry run 通过：`rows=1082`，`duration_s=119.991`，`status=0`，`authenticated=True`，`bias=False`，`reconnect/error=4/3`；Fz 从 `-38.750 N` 到 `-38.720 N`，变化 `+0.030 N`。

主测完成：`rows=258598`，`duration_s=28800.078`，`status=0`，`authenticated=True`，`bias=False`，`reconnect/error=949/948`。Fz 从 `-38.870 N` 到 `-37.810 N`，首末变化 `+1.060 N`；Fz 均值 `-38.345 N`，std `0.344 N`。

08h checkpoint 严格按 `t_s <= 28800` 截取，`rows=258597`；主测 summary 包含边界后的最后一个样本，`rows=258598`，所以两者末值可能差一个采样点。这不是协议差异。

`events.jsonl` 中事件计数为：`version_ok=1`，`socket_handshake_ok=949`，`poll_http_error=948`，`status=15`，`logger_finished=1`。这里的 `poll_http_error 400` 与 Socket.IO polling 会话重建相伴出现；由于主 CSV 持续写入且最终有 `logger_finished`，本报告不把它判为不可恢复连接失败。

每小时 checkpoint 统一表：

说明：checkpoint 目录只复制主 CSV 的截断数据，不复制 `events.jsonl`，所以 hourly 表中的 `reconnect/error` 标为 `N/A`。事件计数以主测 `main_8h/summary.json` 为准。

| 快照 | 时长 (s) | 样本数 | Status | Auth | Bias | reconnect/error | Fz首值 (N) | Fz末值 (N) | Fz变化 (N) | Fz均值 (N) | Fz std (N) |
|---|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|
| 01h | 3599.9 | 32333 | 0 | True | False | N/A | -38.870 | -38.900 | -0.030 | -38.798 | 0.241 |
| 02h | 7200.0 | 64659 | 0 | True | False | N/A | -38.870 | -38.820 | +0.050 | -38.712 | 0.266 |
| 03h | 10800.0 | 96978 | 0 | True | False | N/A | -38.870 | -38.130 | +0.740 | -38.609 | 0.293 |
| 04h | 14399.9 | 129305 | 0 | True | False | N/A | -38.870 | -38.540 | +0.330 | -38.557 | 0.292 |
| 05h | 17999.9 | 161626 | 0 | True | False | N/A | -38.870 | -38.160 | +0.710 | -38.502 | 0.303 |
| 06h | 21599.9 | 193952 | 0 | True | False | N/A | -38.870 | -38.290 | +0.580 | -38.442 | 0.320 |
| 07h | 25199.9 | 226270 | 0 | True | False | N/A | -38.870 | -38.070 | +0.800 | -38.394 | 0.330 |
| 08h | 28800.0 | 258597 | 0 | True | False | N/A | -38.870 | -37.600 | +1.270 | -38.345 | 0.344 |

主测完整轴统计：

| main_8h | 轴 | 均值 | Std | Min | Max | 首值 | 末值 | 末-首 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| main_8h | Fx (N) | 0.367 | 0.088 | -0.050 | 0.840 | 0.600 | 0.390 | -0.210 |
| main_8h | Fy (N) | 3.368 | 0.046 | 3.160 | 3.570 | 3.350 | 3.360 | +0.010 |
| main_8h | Fz (N) | -38.345 | 0.344 | -39.640 | -36.980 | -38.870 | -37.810 | +1.060 |
| main_8h | Tx (Nm) | -0.116 | 0.007 | -0.156 | -0.085 | -0.135 | -0.117 | +0.018 |
| main_8h | Ty (Nm) | -0.079 | 0.009 | -0.111 | -0.041 | -0.071 | -0.089 | -0.018 |
| main_8h | Tz (Nm) | 0.088 | 0.004 | 0.068 | 0.105 | 0.080 | 0.093 | +0.013 |

主测 10 分钟 bin 选点如下；`相对前10min基线` 使用主测最初 600 s 的均值作为零参考，只用于看趋势，不代表传感器被清零。

| 区间 (s) | 样本数 | Fz均值 (N) | 相对前10min基线 (N) | Fx均值 (N) | Fy均值 (N) |
|---:|---:|---:|---:|---:|---:|
| 0-600 | 5392 | -38.763 | +0.000 | 0.567 | 3.369 |
| 1200-1800 | 5388 | -38.777 | -0.015 | 0.455 | 3.351 |
| 3000-3600 | 5387 | -38.874 | -0.111 | 0.307 | 3.380 |
| 6600-7200 | 5389 | -38.434 | +0.328 | 0.341 | 3.357 |
| 13800-14400 | 5387 | -38.384 | +0.379 | 0.365 | 3.377 |
| 21000-21600 | 5386 | -38.086 | +0.677 | 0.379 | 3.368 |
| 28200-28800 | 5388 | -37.933 | +0.830 | 0.414 | 3.374 |

## 结论

本次 mounted sensor-only 8 小时只读主测完整完成，最终 `status=0`、`authenticated=True`、`bias=False`，每小时 checkpoint 从 01h 到 08h 均已生成。主测期间未调用任何清零、bias、autocalib、配置写入或机器人运动命令。

按主测首末样本看，Fz 从 `-38.870 N` 到 `-37.810 N`，变化 `+1.060 N`。更稳健地看，hourly checkpoint 的 Fz 均值从 01h 的 `-38.798 N` 逐步到 08h 的 `-38.345 N`；这说明本 run 的主要变化应按长时间均值和图形趋势判断，而不是只看某一个末端样本。

本实验的限制是：开跑前没有记录 `P_work_drift` 六轴角，报告不能把漂移和具体姿态做定量绑定；本 run 也没有温度通道，因此这里称为上电后长时读数漂移，不把它直接等同于温度模型。

## 下一步

下一次正式对比应记录 `P_work_drift` 六轴角，并在同一 mounted sensor-only 条件下重复一条短 run 或同姿态 8h run，用来判断这次 Fz 均值变化是否可重复。若要进一步进入接触实验，需要先完成低速无接触 cable sweep，并记录真实 TCP、payload 和传感器安装方向。

## Appendix A. 每小时 checkpoint 图片

### 01h

![01h full force/torque](checkpoints/01h/force_torque.png)

![01h Fz](checkpoints/01h/fz.png)

![01h Fx/Fy](checkpoints/01h/fxy.png)

### 02h

![02h full force/torque](checkpoints/02h/force_torque.png)

![02h Fz](checkpoints/02h/fz.png)

![02h Fx/Fy](checkpoints/02h/fxy.png)

### 03h

![03h full force/torque](checkpoints/03h/force_torque.png)

![03h Fz](checkpoints/03h/fz.png)

![03h Fx/Fy](checkpoints/03h/fxy.png)

### 04h

![04h full force/torque](checkpoints/04h/force_torque.png)

![04h Fz](checkpoints/04h/fz.png)

![04h Fx/Fy](checkpoints/04h/fxy.png)

### 05h

![05h full force/torque](checkpoints/05h/force_torque.png)

![05h Fz](checkpoints/05h/fz.png)

![05h Fx/Fy](checkpoints/05h/fxy.png)

### 06h

![06h full force/torque](checkpoints/06h/force_torque.png)

![06h Fz](checkpoints/06h/fz.png)

![06h Fx/Fy](checkpoints/06h/fxy.png)

### 07h

![07h full force/torque](checkpoints/07h/force_torque.png)

![07h Fz](checkpoints/07h/fz.png)

![07h Fx/Fy](checkpoints/07h/fxy.png)

### 08h

![08h full force/torque](checkpoints/08h/force_torque.png)

![08h Fz](checkpoints/08h/fz.png)

![08h Fx/Fy](checkpoints/08h/fxy.png)

## Appendix B. 完整轴统计

| 快照 | 轴 | 均值 | Std | Min | Max | 首值 | 末值 | 末-首 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 01h | Fx (N) | 0.425 | 0.110 | 0.020 | 0.840 | 0.600 | 0.330 | -0.270 |
| 01h | Fy (N) | 3.366 | 0.046 | 3.160 | 3.540 | 3.350 | 3.460 | +0.110 |
| 01h | Fz (N) | -38.798 | 0.241 | -39.640 | -37.790 | -38.870 | -38.900 | -0.030 |
| 01h | Tx (Nm) | -0.122 | 0.008 | -0.156 | -0.091 | -0.135 | -0.118 | +0.017 |
| 01h | Ty (Nm) | -0.067 | 0.006 | -0.099 | -0.041 | -0.071 | -0.062 | +0.009 |
| 01h | Tz (Nm) | 0.082 | 0.003 | 0.068 | 0.097 | 0.080 | 0.082 | +0.002 |
| 02h | Fx (N) | 0.361 | 0.113 | -0.050 | 0.840 | 0.600 | 0.420 | -0.180 |
| 02h | Fy (N) | 3.364 | 0.046 | 3.160 | 3.540 | 3.350 | 3.360 | +0.010 |
| 02h | Fz (N) | -38.712 | 0.266 | -39.640 | -37.570 | -38.870 | -38.820 | +0.050 |
| 02h | Tx (Nm) | -0.120 | 0.008 | -0.156 | -0.088 | -0.135 | -0.125 | +0.010 |
| 02h | Ty (Nm) | -0.069 | 0.007 | -0.106 | -0.041 | -0.071 | -0.080 | -0.009 |
| 02h | Tz (Nm) | 0.083 | 0.004 | 0.068 | 0.102 | 0.080 | 0.088 | +0.008 |
| 03h | Fx (N) | 0.349 | 0.103 | -0.050 | 0.840 | 0.600 | 0.330 | -0.270 |
| 03h | Fy (N) | 3.364 | 0.046 | 3.160 | 3.550 | 3.350 | 3.380 | +0.030 |
| 03h | Fz (N) | -38.609 | 0.293 | -39.640 | -37.460 | -38.870 | -38.130 | +0.740 |
| 03h | Tx (Nm) | -0.118 | 0.008 | -0.156 | -0.087 | -0.135 | -0.120 | +0.015 |
| 03h | Ty (Nm) | -0.072 | 0.008 | -0.106 | -0.041 | -0.071 | -0.082 | -0.011 |
| 03h | Tz (Nm) | 0.085 | 0.004 | 0.068 | 0.102 | 0.080 | 0.090 | +0.010 |
| 04h | Fx (N) | 0.351 | 0.097 | -0.050 | 0.840 | 0.600 | 0.470 | -0.130 |
| 04h | Fy (N) | 3.367 | 0.046 | 3.160 | 3.570 | 3.350 | 3.370 | +0.020 |
| 04h | Fz (N) | -38.557 | 0.292 | -39.640 | -37.450 | -38.870 | -38.540 | +0.330 |
| 04h | Tx (Nm) | -0.118 | 0.008 | -0.156 | -0.087 | -0.135 | -0.118 | +0.017 |
| 04h | Ty (Nm) | -0.074 | 0.009 | -0.106 | -0.041 | -0.071 | -0.087 | -0.016 |
| 04h | Tz (Nm) | 0.086 | 0.004 | 0.068 | 0.104 | 0.080 | 0.092 | +0.012 |
| 05h | Fx (N) | 0.354 | 0.093 | -0.050 | 0.840 | 0.600 | 0.460 | -0.140 |
| 05h | Fy (N) | 3.368 | 0.046 | 3.160 | 3.570 | 3.350 | 3.390 | +0.040 |
| 05h | Fz (N) | -38.502 | 0.303 | -39.640 | -37.380 | -38.870 | -38.160 | +0.710 |
| 05h | Tx (Nm) | -0.117 | 0.008 | -0.156 | -0.087 | -0.135 | -0.123 | +0.012 |
| 05h | Ty (Nm) | -0.076 | 0.009 | -0.108 | -0.041 | -0.071 | -0.089 | -0.018 |
| 05h | Tz (Nm) | 0.086 | 0.005 | 0.068 | 0.104 | 0.080 | 0.093 | +0.013 |
| 06h | Fx (N) | 0.357 | 0.090 | -0.050 | 0.840 | 0.600 | 0.490 | -0.110 |
| 06h | Fy (N) | 3.368 | 0.046 | 3.160 | 3.570 | 3.350 | 3.350 | +0.000 |
| 06h | Fz (N) | -38.442 | 0.320 | -39.640 | -37.170 | -38.870 | -38.290 | +0.580 |
| 06h | Tx (Nm) | -0.117 | 0.007 | -0.156 | -0.085 | -0.135 | -0.119 | +0.016 |
| 06h | Ty (Nm) | -0.077 | 0.009 | -0.109 | -0.041 | -0.071 | -0.087 | -0.016 |
| 06h | Tz (Nm) | 0.087 | 0.005 | 0.068 | 0.104 | 0.080 | 0.092 | +0.012 |
| 07h | Fx (N) | 0.362 | 0.089 | -0.050 | 0.840 | 0.600 | 0.450 | -0.150 |
| 07h | Fy (N) | 3.368 | 0.046 | 3.160 | 3.570 | 3.350 | 3.450 | +0.100 |
| 07h | Fz (N) | -38.394 | 0.330 | -39.640 | -37.170 | -38.870 | -38.070 | +0.800 |
| 07h | Tx (Nm) | -0.116 | 0.007 | -0.156 | -0.085 | -0.135 | -0.129 | +0.006 |
| 07h | Ty (Nm) | -0.078 | 0.009 | -0.109 | -0.041 | -0.071 | -0.092 | -0.021 |
| 07h | Tz (Nm) | 0.087 | 0.005 | 0.068 | 0.105 | 0.080 | 0.097 | +0.017 |
| 08h | Fx (N) | 0.367 | 0.088 | -0.050 | 0.840 | 0.600 | 0.460 | -0.140 |
| 08h | Fy (N) | 3.368 | 0.046 | 3.160 | 3.570 | 3.350 | 3.410 | +0.060 |
| 08h | Fz (N) | -38.345 | 0.344 | -39.640 | -36.980 | -38.870 | -37.600 | +1.270 |
| 08h | Tx (Nm) | -0.116 | 0.007 | -0.156 | -0.085 | -0.135 | -0.124 | +0.011 |
| 08h | Ty (Nm) | -0.079 | 0.009 | -0.111 | -0.041 | -0.071 | -0.095 | -0.024 |
| 08h | Tz (Nm) | 0.088 | 0.004 | 0.068 | 0.105 | 0.080 | 0.093 | +0.013 |

## Appendix C. 产物链接

| 阶段 | raw CSV | summary | 10 min bins | full 图 | Fz 图 | fxy 图 |
|---|---|---|---|---|---|---|
| dry_run_2min | [raw_wrench.csv](dry_run_2min/raw_wrench.csv) | [summary.json](dry_run_2min/summary.json) | [drift_10min_bins.csv](dry_run_2min/drift_10min_bins.csv) | [force_torque.png](dry_run_2min/force_torque.png) | [fz.png](dry_run_2min/fz.png) | [fxy.png](dry_run_2min/fxy.png) |
| main_8h | [raw_wrench.csv](main_8h/raw_wrench.csv) | [summary.json](main_8h/summary.json) | [drift_10min_bins.csv](main_8h/drift_10min_bins.csv) | [force_torque.png](main_8h/force_torque.png) | [fz.png](main_8h/fz.png) | [fxy.png](main_8h/fxy.png) |
| 01h | [raw_wrench.csv](checkpoints/01h/raw_wrench.csv) | [summary.json](checkpoints/01h/summary.json) | [drift_10min_bins.csv](checkpoints/01h/drift_10min_bins.csv) | [force_torque.png](checkpoints/01h/force_torque.png) | [fz.png](checkpoints/01h/fz.png) | [fxy.png](checkpoints/01h/fxy.png) |
| 02h | [raw_wrench.csv](checkpoints/02h/raw_wrench.csv) | [summary.json](checkpoints/02h/summary.json) | [drift_10min_bins.csv](checkpoints/02h/drift_10min_bins.csv) | [force_torque.png](checkpoints/02h/force_torque.png) | [fz.png](checkpoints/02h/fz.png) | [fxy.png](checkpoints/02h/fxy.png) |
| 03h | [raw_wrench.csv](checkpoints/03h/raw_wrench.csv) | [summary.json](checkpoints/03h/summary.json) | [drift_10min_bins.csv](checkpoints/03h/drift_10min_bins.csv) | [force_torque.png](checkpoints/03h/force_torque.png) | [fz.png](checkpoints/03h/fz.png) | [fxy.png](checkpoints/03h/fxy.png) |
| 04h | [raw_wrench.csv](checkpoints/04h/raw_wrench.csv) | [summary.json](checkpoints/04h/summary.json) | [drift_10min_bins.csv](checkpoints/04h/drift_10min_bins.csv) | [force_torque.png](checkpoints/04h/force_torque.png) | [fz.png](checkpoints/04h/fz.png) | [fxy.png](checkpoints/04h/fxy.png) |
| 05h | [raw_wrench.csv](checkpoints/05h/raw_wrench.csv) | [summary.json](checkpoints/05h/summary.json) | [drift_10min_bins.csv](checkpoints/05h/drift_10min_bins.csv) | [force_torque.png](checkpoints/05h/force_torque.png) | [fz.png](checkpoints/05h/fz.png) | [fxy.png](checkpoints/05h/fxy.png) |
| 06h | [raw_wrench.csv](checkpoints/06h/raw_wrench.csv) | [summary.json](checkpoints/06h/summary.json) | [drift_10min_bins.csv](checkpoints/06h/drift_10min_bins.csv) | [force_torque.png](checkpoints/06h/force_torque.png) | [fz.png](checkpoints/06h/fz.png) | [fxy.png](checkpoints/06h/fxy.png) |
| 07h | [raw_wrench.csv](checkpoints/07h/raw_wrench.csv) | [summary.json](checkpoints/07h/summary.json) | [drift_10min_bins.csv](checkpoints/07h/drift_10min_bins.csv) | [force_torque.png](checkpoints/07h/force_torque.png) | [fz.png](checkpoints/07h/fz.png) | [fxy.png](checkpoints/07h/fxy.png) |
| 08h | [raw_wrench.csv](checkpoints/08h/raw_wrench.csv) | [summary.json](checkpoints/08h/summary.json) | [drift_10min_bins.csv](checkpoints/08h/drift_10min_bins.csv) | [force_torque.png](checkpoints/08h/force_torque.png) | [fz.png](checkpoints/08h/fz.png) | [fxy.png](checkpoints/08h/fxy.png) |

## Appendix D. Checkpoint 脚本

```python
#!/usr/bin/env python3
"""Create hourly OnRobot drift checkpoints from a running main CSV."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path


def read_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def row_time(row: dict[str, str]) -> float | None:
    try:
        return float(row.get("t_s", ""))
    except ValueError:
        return None


def write_checkpoint(
    fieldnames: list[str],
    rows: list[dict[str, str]],
    checkpoint_dir: Path,
) -> None:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    csv_path = checkpoint_dir / "raw_wrench.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--main-dir", required=True)
    parser.add_argument("--hours", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    experiment_root = Path(args.experiment_root).resolve()
    run_root = Path(args.run_root).resolve()
    main_dir = Path(args.main_dir).resolve()
    raw_csv = main_dir / "raw_wrench.csv"
    if not raw_csv.exists():
        print(f"checkpoint_wait raw_missing {raw_csv}", flush=True)
        return 0

    fieldnames, rows = read_rows(raw_csv)
    if not rows:
        print("checkpoint_wait rows=0", flush=True)
        return 0

    times = [t for t in (row_time(row) for row in rows) if t is not None]
    if not times:
        print("checkpoint_wait no_t_s", flush=True)
        return 0
    max_t = max(times)

    checkpoints_root = run_root / "checkpoints"
    checkpoints_root.mkdir(parents=True, exist_ok=True)
    created = []
    for hour in range(1, args.hours + 1):
        threshold = hour * 3600.0
        if max_t < threshold:
            continue
        name = f"{hour:02d}h"
        checkpoint_dir = checkpoints_root / name
        summary_path = checkpoint_dir / "summary.json"
        if summary_path.exists() and not args.force:
            continue
        subset = [row for row in rows if (row_time(row) is not None and row_time(row) <= threshold)]
        if not subset:
            continue
        write_checkpoint(fieldnames, subset, checkpoint_dir)
        subprocess.run(
            [
                "python3",
                str(experiment_root / "tools" / "analyze_onrobot_drift.py"),
                str(checkpoint_dir),
                "--baseline-s",
                "600",
                "--bin-s",
                "600",
            ],
            cwd=str(experiment_root),
            check=True,
        )
        created.append({"name": name, "rows": len(subset), "threshold_s": threshold})

    manifest = {
        "main_dir": str(main_dir),
        "max_t_s": max_t,
        "rows_seen": len(rows),
        "created_or_updated": created,
    }
    (checkpoints_root / "checkpoint_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    if created:
        print("checkpoint_created " + json.dumps(created), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```
