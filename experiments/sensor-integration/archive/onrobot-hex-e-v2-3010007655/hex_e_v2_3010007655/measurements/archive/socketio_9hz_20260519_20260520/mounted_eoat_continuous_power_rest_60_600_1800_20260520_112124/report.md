# OnRobot HEX-E 自研末端安装后 60/600/1800 秒连续上电静置恢复测试

## 实验目的

本次实验记录 OnRobot HEX-E 安装在 UR10e 且已装上自研末端后的只读输出。UR10e 未开机，本实验不读 UR 状态、不发运动命令，只读取 OnRobot Compute Box 的 `/version` 与 Socket.IO force/torque stream。

本次段间不是断电冷却，而是 `continuous-power rest/recovery`：Compute Box 持续上电。原计划第三段为 3600 s，但该段测量中用户触碰到装置，因此取消并改为新的 1800 s 有效段。报告中的 60 s、600 s、1800 s 只能解释为同一连续上电历史下的分段读数，不能解释为三个独立冷启动。

## 设备与实验条件

| 项目 | 内容 |
|---|---|
| 传感器 | OnRobot HEX-E v2，live serial 由各段 summary 记录 |
| 自研末端 | 已安装在 HEX-E/UR10e 末端侧 |
| UR10e | 未开机；不读 Dashboard/RTDE/ROS；不运动 |
| Compute Box | `192.168.1.1`，预检 `/version` 为 `4.1.8` |
| Ubuntu 网口 | `enp3s0`，`192.168.1.10/24` |
| 运行目录 | `/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_3600_20260520_112124` |
| 段间状态 | Compute Box 持续上电；R00 完整静置 30 min；R01 因用户要求直接继续而提前结束 |
| 清零/偏置 | 未调用 UR `zero_ftsensor()`；未调用 OnRobot zero/bias/autocalib |
| 安全边界 | 无 firmware、DIP switch、配置写入、TCP/payload/gravity/ROS/UR 配置修改 |
| 接触状态 | 没有设计接触动作；是否完全无环境接触以现场安装状态为准 |

预检记录见 [preflight_notes.md](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/preflight_notes.md)，协议 manifest 见 [protocol_manifest.json](protocol_manifest.json)。

## 实验命令

三段有效测量均使用同一个只读 logger，`<SEGMENT>` 分别为 `S00_60s`、`S01_600s`、`S02_1800s`：

```bash
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s <60|600|1800> \
  --out-dir "/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_3600_20260520_112124/segments/<SEGMENT>" \
  --flush-every-s 10 \
  --status-every-s <30|120|300> \
  --baseline-s 60 \
  --bin-s 60
```

每段结束后统一重分析：

```bash
python3 tools/analyze_onrobot_drift.py "$SEGMENT_DIR" --baseline-s 60 --bin-s 60
```

## 数据与图片

### S00_60s

![S00_60s full force/torque](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S00_60s/force_torque.png)

![S00_60s Fz](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S00_60s/fz.png)

![S00_60s Fx/Fy](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S00_60s/fxy.png)

### S01_600s

![S01_600s full force/torque](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S01_600s/force_torque.png)

![S01_600s Fz](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S01_600s/fz.png)

![S01_600s Fx/Fy](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S01_600s/fxy.png)

### S02_1800s

![S02_1800s full force/torque](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_1800s/force_torque.png)

![S02_1800s Fz](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_1800s/fz.png)

![S02_1800s Fx/Fy](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_1800s/fxy.png)

## 统计结果

Protocol table:

| Segment | Duration (s) | Samples | Power state | Rest before | Rest after | UR10e state | Status/Auth/Bias | reconnect/error |
|---|---:|---:|---|---:|---:|---|---|---:|
| S00_60s | 60.0 | 543 | continuous_on | N/A | 1800.0 | off_not_read | 0/True/False | 2/1 |
| S01_600s | 600.0 | 5394 | continuous_on | 1800.0 | 1080.9 | off_not_read | 0/True/False | 20/19 |
| S02_1800s | 1799.9 | 16168 | continuous_on | 1080.9 | N/A | off_not_read | 0/True/False | 60/59 |

被取消片段：

| Segment | Planned duration (s) | Rows before abort | Last t_s (s) | Valid for report | Reason |
|---|---:|---:|---:|---|---|
| S02_3600s | 3600 | 2599 | 289.222 | False | user touched the setup during this measurement and requested cancellation |

被取消片段保留原始 CSV 和中断记录：[S02_3600s/raw_wrench.csv](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_3600s/raw_wrench.csv)，[S02_3600s/ABORTED_TOUCHED.json](ABORTED_TOUCHED.json)。

Fz 统一表：

| Segment | Fz首值 (N) | Fz末值 (N) | 末-首 (N) | Fz均值 (N) | Std (N) | Min (N) | Max (N) |
|---|---:|---:|---:|---:|---:|---:|---:|
| S00_60s | -37.350 | -37.230 | +0.120 | -37.291 | 0.205 | -37.930 | -36.490 |
| S01_600s | -37.500 | -37.130 | +0.370 | -37.088 | 0.216 | -37.970 | -36.310 |
| S02_1800s | -36.720 | -36.960 | -0.240 | -36.899 | 0.223 | -37.790 | -36.010 |

完整轴统计：

| Segment | Axis | Mean | Std | Min | Max | First | Last | Last-First |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| S00_60s | Fx (N) | 0.480 | 0.074 | 0.250 | 0.690 | 0.380 | 0.530 | +0.150 |
| S00_60s | Fy (N) | 3.438 | 0.043 | 3.280 | 3.560 | 3.310 | 3.460 | +0.150 |
| S00_60s | Fz (N) | -37.291 | 0.205 | -37.930 | -36.490 | -37.350 | -37.230 | +0.120 |
| S00_60s | Tx (Nm) | -0.116 | 0.006 | -0.136 | -0.098 | -0.105 | -0.122 | -0.017 |
| S00_60s | Ty (Nm) | -0.097 | 0.007 | -0.114 | -0.077 | -0.087 | -0.099 | -0.012 |
| S00_60s | Tz (Nm) | 0.091 | 0.004 | 0.083 | 0.103 | 0.086 | 0.094 | +0.008 |
| S01_600s | Fx (N) | 0.504 | 0.071 | 0.220 | 0.780 | 0.630 | 0.360 | -0.270 |
| S01_600s | Fy (N) | 3.452 | 0.043 | 3.300 | 3.600 | 3.430 | 3.420 | -0.010 |
| S01_600s | Fz (N) | -37.088 | 0.216 | -37.970 | -36.310 | -37.500 | -37.130 | +0.370 |
| S01_600s | Tx (Nm) | -0.119 | 0.007 | -0.143 | -0.093 | -0.123 | -0.107 | +0.016 |
| S01_600s | Ty (Nm) | -0.100 | 0.007 | -0.123 | -0.075 | -0.104 | -0.086 | +0.018 |
| S01_600s | Tz (Nm) | 0.093 | 0.004 | 0.080 | 0.105 | 0.094 | 0.088 | -0.006 |
| S02_1800s | Fx (N) | 0.470 | 0.072 | 0.170 | 0.720 | 0.360 | 0.460 | +0.100 |
| S02_1800s | Fy (N) | 3.435 | 0.045 | 3.260 | 3.610 | 3.440 | 3.390 | -0.050 |
| S02_1800s | Fz (N) | -36.899 | 0.223 | -37.790 | -36.010 | -36.720 | -36.960 | -0.240 |
| S02_1800s | Tx (Nm) | -0.116 | 0.007 | -0.146 | -0.089 | -0.111 | -0.107 | +0.004 |
| S02_1800s | Ty (Nm) | -0.096 | 0.007 | -0.127 | -0.070 | -0.090 | -0.094 | -0.004 |
| S02_1800s | Tz (Nm) | 0.092 | 0.003 | 0.077 | 0.108 | 0.091 | 0.089 | -0.002 |

## 60 秒 bin 趋势

### S00_60s

| 区间 (s) | 样本数 | Fz均值 (N) | 相对本段前60s基线 (N) | Fx均值 (N) | Fy均值 (N) |
|---:|---:|---:|---:|---:|---:|
| 0-60 | 542 | -37.291 | +0.000 | 0.480 | 3.438 |
| 60-120 | 1 | -37.230 | +0.061 | 0.530 | 3.460 |

### S01_600s

| 区间 (s) | 样本数 | Fz均值 (N) | 相对本段前60s基线 (N) | Fx均值 (N) | Fy均值 (N) |
|---:|---:|---:|---:|---:|---:|
| 0-60 | 543 | -37.055 | +0.000 | 0.516 | 3.457 |
| 60-120 | 539 | -37.054 | +0.001 | 0.521 | 3.454 |
| 120-180 | 540 | -37.055 | +0.000 | 0.520 | 3.455 |
| 180-240 | 538 | -37.022 | +0.033 | 0.514 | 3.454 |
| 240-300 | 539 | -37.058 | -0.002 | 0.504 | 3.450 |
| 300-360 | 539 | -37.117 | -0.062 | 0.502 | 3.453 |
| 360-420 | 539 | -37.159 | -0.103 | 0.492 | 3.454 |
| 420-480 | 539 | -37.157 | -0.102 | 0.493 | 3.450 |
| 480-540 | 538 | -37.108 | -0.053 | 0.486 | 3.446 |
| 540-600 | 539 | -37.100 | -0.045 | 0.491 | 3.447 |
| 600-660 | 1 | -37.130 | -0.075 | 0.360 | 3.420 |

### S02_1800s

| 区间 (s) | 样本数 | Fz均值 (N) | 相对本段前60s基线 (N) | Fx均值 (N) | Fy均值 (N) |
|---:|---:|---:|---:|---:|---:|
| 0-60 | 541 | -36.856 | +0.000 | 0.452 | 3.451 |
| 60-120 | 540 | -36.872 | -0.015 | 0.450 | 3.441 |
| 120-180 | 538 | -36.940 | -0.083 | 0.459 | 3.440 |
| 180-240 | 539 | -36.884 | -0.027 | 0.458 | 3.437 |
| 240-300 | 538 | -36.887 | -0.031 | 0.460 | 3.436 |
| 300-360 | 539 | -36.935 | -0.079 | 0.468 | 3.444 |
| 360-420 | 540 | -36.908 | -0.051 | 0.460 | 3.440 |
| 420-480 | 539 | -36.907 | -0.050 | 0.459 | 3.443 |
| 480-540 | 538 | -36.883 | -0.026 | 0.459 | 3.439 |
| 540-600 | 539 | -36.867 | -0.010 | 0.456 | 3.435 |
| 600-660 | 538 | -36.895 | -0.039 | 0.462 | 3.435 |
| 660-720 | 539 | -36.873 | -0.017 | 0.460 | 3.435 |
| 720-780 | 540 | -36.882 | -0.026 | 0.464 | 3.434 |
| 780-840 | 538 | -36.880 | -0.023 | 0.465 | 3.430 |
| 840-900 | 539 | -36.907 | -0.051 | 0.470 | 3.429 |
| 900-960 | 538 | -36.899 | -0.043 | 0.469 | 3.429 |
| 960-1020 | 539 | -36.865 | -0.008 | 0.480 | 3.431 |
| 1020-1080 | 538 | -36.924 | -0.068 | 0.482 | 3.428 |
| 1080-1140 | 539 | -36.898 | -0.041 | 0.475 | 3.430 |
| 1140-1200 | 539 | -36.883 | -0.027 | 0.480 | 3.433 |
| 1200-1260 | 540 | -36.866 | -0.009 | 0.476 | 3.431 |
| 1260-1320 | 539 | -36.844 | +0.012 | 0.479 | 3.430 |
| 1320-1380 | 538 | -36.854 | +0.003 | 0.473 | 3.431 |
| 1380-1440 | 539 | -36.887 | -0.031 | 0.479 | 3.429 |
| 1440-1500 | 538 | -36.920 | -0.063 | 0.479 | 3.433 |
| 1500-1560 | 539 | -36.961 | -0.105 | 0.480 | 3.432 |
| 1560-1620 | 538 | -36.984 | -0.128 | 0.479 | 3.429 |
| 1620-1680 | 539 | -36.914 | -0.058 | 0.483 | 3.433 |
| 1680-1740 | 540 | -36.944 | -0.087 | 0.488 | 3.437 |
| 1740-1800 | 539 | -36.960 | -0.104 | 0.498 | 3.442 |
| 1800-1860 | 1 | -36.960 | -0.104 | 0.460 | 3.390 |

## 结论

本次协议完成状态：`protocol_done=True`，`protocol_failed=0`。三段有效采集均在 UR10e 关机、Compute Box 连续上电的条件下完成。`S02_3600s` 因触碰被取消，不进入有效统计比较。

从第一段首样本到第三段末样本，Fz 从 `-37.350 N` 到 `-36.960 N`。这个跨度包含两次 30 min 上电静置，不能当作单段漂移斜率。

本次最重要的解释边界是：段间没有断电，因此结果反映 mounted EOAT 在 continuous-power 条件下的恢复/再稳定过程，不是严格温度冷却曲线。

## 下一步

若要得到真正冷却对比，需要加入可远程控制或定时的 Compute Box 24 V 电源开关；否则后续仍应把段间状态写作 continuous-power rest。若要做接触实验，需先完成低速无接触 cable sweep，并记录 TCP、payload、安装方向和现场接触状态。

## Appendix A. 产物链接

| Segment | raw CSV | summary | bins | full 图 | Fz 图 | fxy 图 |
|---|---|---|---|---|---|---|
| S00_60s | [raw_wrench.csv](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S00_60s/raw_wrench.csv) | [summary.json](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S00_60s/summary.json) | [drift_10min_bins.csv](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S00_60s/drift_10min_bins.csv) | [force_torque.png](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S00_60s/force_torque.png) | [fz.png](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S00_60s/fz.png) | [fxy.png](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S00_60s/fxy.png) |
| S01_600s | [raw_wrench.csv](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S01_600s/raw_wrench.csv) | [summary.json](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S01_600s/summary.json) | [drift_10min_bins.csv](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S01_600s/drift_10min_bins.csv) | [force_torque.png](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S01_600s/force_torque.png) | [fz.png](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S01_600s/fz.png) | [fxy.png](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S01_600s/fxy.png) |
| S02_1800s | [raw_wrench.csv](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_1800s/raw_wrench.csv) | [summary.json](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_1800s/summary.json) | [drift_10min_bins.csv](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_1800s/drift_10min_bins.csv) | [force_torque.png](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_1800s/force_torque.png) | [fz.png](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_1800s/fz.png) | [fxy.png](experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_1800_20260520_112124/segments/S02_1800s/fxy.png) |

## Appendix B. Protocol Manifest

```json
{
  "run_root": "/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/mounted_eoat_continuous_power_rest_60_600_3600_20260520_112124",
  "experiment_root": "/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655",
  "host": "192.168.1.1",
  "protocol": "continuous_power_rest_recovery",
  "not_true_power_cycle_cooling": true,
  "ur10e_state": "off_not_read",
  "segments": [
    {
      "name": "S00_60s",
      "duration_s": 60,
      "status_every_s": 30
    },
    {
      "name": "S01_600s",
      "duration_s": 600,
      "status_every_s": 120
    },
    {
      "name": "S02_3600s",
      "duration_s": 3600,
      "status_every_s": 600
    }
  ],
  "rests": [
    {
      "name": "R00_rest_30min",
      "after": "S00_60s",
      "duration_s": 1800
    },
    {
      "name": "R01_rest_30min",
      "after": "S01_600s",
      "duration_s": 1800,
      "completed_planned_rest": false,
      "actual_duration_s": 1080.8698186019974
    }
  ],
  "read_only_policy": [
    "no_ur_motion",
    "no_ur_zero_ftsensor",
    "no_onrobot_zero",
    "no_onrobot_bias",
    "no_autocalib",
    "no_firmware",
    "no_config_write"
  ],
  "effective_protocol": "continuous_power_rest_recovery_60_600_1800",
  "original_requested_segments": [
    {
      "name": "S00_60s",
      "duration_s": 60
    },
    {
      "name": "S01_600s",
      "duration_s": 600
    },
    {
      "name": "S02_3600s",
      "duration_s": 3600
    }
  ],
  "effective_valid_segments": [
    {
      "name": "S00_60s",
      "duration_s": 60
    },
    {
      "name": "S01_600s",
      "duration_s": 600
    },
    {
      "name": "S02_1800s",
      "duration_s": 1800
    }
  ],
  "aborted_segments": [
    {
      "name": "S02_3600s",
      "planned_duration_s": 3600,
      "reason": "user touched the setup during measurement and requested cancellation"
    }
  ]
}
```
