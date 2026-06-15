# OnRobot HEX-E 7 小时只读零漂记录

## 实验目的

本次实验验证 OnRobot HEX-E v2 在不接触、不装到 UR10e、不执行 bias/zero/autocalib 的条件下，长时间只读输出是否稳定。实验先用 20 秒测试确认 Compute Box、Socket.IO 读数和脚本工作正常；通过后连续采集 7 小时，并在 2 h、4 h、6 h、7 h 生成独立快照。

这份报告只讨论 OnRobot 传感器和 Compute Box 的只读数据。UR10e 末端是否挂载负载不进入本次数据解释，因为传感器没有安装在 UR10e 上，也没有机器人运动。

## 设备与实验条件

| 项目 | 内容 |
|---|---|
| 传感器 | OnRobot HEX-E v2，live serial: `HEXEB806` |
| 文档/包名标识 | `3010007655`；历史材料中还出现 `HEXEB306`，后续归档时需要保留这个不一致 |
| Compute Box | `192.168.1.1`，`/version` 返回 `4.1.8` |
| Ubuntu 网口 | `enp3s0`，`192.168.1.10/24` |
| 连接方式 | HEX-E 传感器接 Compute Box，Compute Box 通过网线接 Ubuntu |
| 供电 | Compute Box 24 V 适配器供电 |
| 安全边界 | 未安装到 UR10e；未连接裸线机器人电源；未调用 bias、zero、autocalib、firmware 或配置接口 |
| 采样脚本 | `tools/onrobot_socketio_logger.py` |
| 主测时长 | 25200 s，约 7 h |
| 分段保存 | 2 h、4 h、6 h、7 h final |

## 实验流程

先运行 20 秒只读测试，确认 Socket.IO 数据、`status`、`authenticated` 和 `bias` 状态正常；随后运行 25200 s 主测。2 h、4 h、6 h、7 h final 快照都由同一份 `main_7h/raw_wrench.csv` 按时间截取并重新分析，所以 CSV、统计和图片来自同一条连续主测数据，比较口径一致。完整命令放在附录。

## 数据与图片

主测完整图如下。上图为 Fx/Fy/Fz，下图为 Tx/Ty/Tz；横轴是采集开始后的秒数。

![7 小时 OnRobot HEX-E force/torque](main_7h/force_torque.png)

Fz 单独图用于直接判断竖直方向读数的长时漂移。

![7 小时 OnRobot HEX-E Fz](main_7h/fz.png)

fxy 图把 Fx 和 Fy 作为两条曲线放在同一张图里，用来判断水平两个分量是否同步漂移或各自偏移。

![7 小时 OnRobot HEX-E Fx/Fy](main_7h/fxy.png)

阶段快照数据和图片：

| 快照 | 数据 | 统计 | 全量图 | Fz图 | fxy图 |
|---|---|---|---|---|---|
| 2 h | [raw_wrench.csv](checkpoints/2h/raw_wrench.csv) | [summary.json](checkpoints/2h/summary.json) | [force_torque.png](checkpoints/2h/force_torque.png) | [fz.png](checkpoints/2h/fz.png) | [fxy.png](checkpoints/2h/fxy.png) |
| 4 h | [raw_wrench.csv](checkpoints/4h/raw_wrench.csv) | [summary.json](checkpoints/4h/summary.json) | [force_torque.png](checkpoints/4h/force_torque.png) | [fz.png](checkpoints/4h/fz.png) | [fxy.png](checkpoints/4h/fxy.png) |
| 6 h | [raw_wrench.csv](checkpoints/6h/raw_wrench.csv) | [summary.json](checkpoints/6h/summary.json) | [force_torque.png](checkpoints/6h/force_torque.png) | [fz.png](checkpoints/6h/fz.png) | [fxy.png](checkpoints/6h/fxy.png) |
| 7 h final | [raw_wrench.csv](checkpoints/7h_final/raw_wrench.csv) | [summary.json](checkpoints/7h_final/summary.json) | [force_torque.png](checkpoints/7h_final/force_torque.png) | [fz.png](checkpoints/7h_final/fz.png) | [fxy.png](checkpoints/7h_final/fxy.png) |

## 统计结果

20 秒测试通过：183 行，20.024 s，`status=0`，`authenticated=True`，`bias=False`，`error_count=0`，live serial 为 `HEXEB806`。因此进入 7 小时主测。

主测和所有快照的最终状态都是 `status=0 / authenticated=True / bias=False`。`HTTP 400/reconnect` 是 Socket.IO polling 会话约 30 秒重建一次时记录的事件计数；本次 logger 正常退出，主测生成 226251 行，未出现中断导致的失败退出。

| 快照       |  时长 (s) |    样本数 | Serial   | Status | Auth | Bias  | HTTP 400/reconnect | Fz首值 (N) | Fz末值 (N) | Fz变化 (N) | Fz均值 (N) | Fz std (N) |
| -------- | ------: | -----: | -------- | -----: | ---- | ----- | -----------------: | -------: | -------: | -------: | -------: | ---------: |
| 2h       |  7198.7 |  64633 | HEXEB806 |      0 | True | False |            237/238 |  -43.250 |  -40.740 |    2.510 |  -41.332 |      0.775 |
| 4h       | 14399.9 | 129284 | HEXEB806 |      0 | True | False |            474/475 |  -43.250 |  -40.370 |    2.880 |  -41.124 |      0.609 |
| 6h       | 21599.9 | 193925 | HEXEB806 |      0 | True | False |            711/712 |  -43.250 |  -40.500 |    2.750 |  -41.007 |      0.541 |
| 7h final | 25199.9 | 226250 | HEXEB806 |      0 | True | False |            830/831 |  -43.250 |  -40.810 |    2.440 |  -40.967 |      0.519 |

完整轴统计如下，所有快照使用相同字段。

| 快照 | 轴 | 均值 | Std | Min | Max | 首值 | 末值 | 末-首 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 2h | fxN (N) | 0.5015 | 0.0763 | 0.1900 | 0.8500 | 0.5700 | 0.4200 | -0.1500 |
| 2h | fyN (N) | 3.1545 | 0.1142 | 2.7900 | 3.5600 | 3.2600 | 3.2100 | -0.0500 |
| 2h | fzN (N) | -41.3319 | 0.7753 | -44.2300 | -40.1000 | -43.2500 | -40.7400 | 2.5100 |
| 2h | txNm (Nm) | -0.1644 | 0.0120 | -0.2110 | -0.1330 | -0.1700 | -0.1540 | 0.0160 |
| 2h | tyNm (Nm) | 0.0021 | 0.0100 | -0.0370 | 0.0340 | -0.0140 | 0.0090 | 0.0230 |
| 2h | tzNm (Nm) | 0.0703 | 0.0062 | 0.0540 | 0.1020 | 0.0880 | 0.0710 | -0.0170 |
| 4h | fxN (N) | 0.4383 | 0.0965 | 0.0500 | 0.8500 | 0.5700 | 0.2900 | -0.2800 |
| 4h | fyN (N) | 3.1969 | 0.0971 | 2.7900 | 3.5600 | 3.2600 | 3.1100 | -0.1500 |
| 4h | fzN (N) | -41.1235 | 0.6086 | -44.2300 | -39.9500 | -43.2500 | -40.3700 | 2.8800 |
| 4h | txNm (Nm) | -0.1572 | 0.0120 | -0.2110 | -0.1230 | -0.1700 | -0.1430 | 0.0270 |
| 4h | tyNm (Nm) | 0.0080 | 0.0103 | -0.0370 | 0.0420 | -0.0140 | 0.0110 | 0.0250 |
| 4h | tzNm (Nm) | 0.0704 | 0.0050 | 0.0540 | 0.1020 | 0.0880 | 0.0710 | -0.0170 |
| 6h | fxN (N) | 0.4033 | 0.1047 | 0.0100 | 0.8500 | 0.5700 | 0.3500 | -0.2200 |
| 6h | fyN (N) | 3.2160 | 0.0882 | 2.7900 | 3.5600 | 3.2600 | 3.2300 | -0.0300 |
| 6h | fzN (N) | -41.0067 | 0.5408 | -44.2300 | -39.7900 | -43.2500 | -40.5000 | 2.7500 |
| 6h | txNm (Nm) | -0.1535 | 0.0118 | -0.2110 | -0.1170 | -0.1700 | -0.1450 | 0.0250 |
| 6h | tyNm (Nm) | 0.0089 | 0.0094 | -0.0370 | 0.0420 | -0.0140 | 0.0040 | 0.0180 |
| 6h | tzNm (Nm) | 0.0711 | 0.0047 | 0.0540 | 0.1020 | 0.0880 | 0.0760 | -0.0120 |
| 7h final | fxN (N) | 0.3861 | 0.1090 | -0.0200 | 0.8500 | 0.5700 | 0.2800 | -0.2900 |
| 7h final | fyN (N) | 3.2205 | 0.0848 | 2.7900 | 3.5600 | 3.2600 | 3.2400 | -0.0200 |
| 7h final | fzN (N) | -40.9666 | 0.5186 | -44.2300 | -39.6800 | -43.2500 | -40.8100 | 2.4400 |
| 7h final | txNm (Nm) | -0.1518 | 0.0119 | -0.2110 | -0.1090 | -0.1700 | -0.1370 | 0.0330 |
| 7h final | tyNm (Nm) | 0.0093 | 0.0091 | -0.0370 | 0.0420 | -0.0140 | 0.0090 | 0.0230 |
| 7h final | tzNm (Nm) | 0.0712 | 0.0045 | 0.0540 | 0.1020 | 0.0880 | 0.0730 | -0.0150 |

合力模长用于观察整体偏置幅值，不替代各轴漂移判断。

| 快照 | \|F\|均值 (N) | \|F\|Std (N) | \|F\|Min (N) | \|F\|Max (N) | \|F\|首值 (N) | \|F\|末值 (N) | \|F\|末-首 (N) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2h | 41.455 | 0.779 | 40.227 | 44.360 | 43.376 | 40.868 | -2.508 |
| 4h | 41.250 | 0.610 | 40.080 | 44.360 | 43.376 | 40.491 | -2.886 |
| 6h | 41.135 | 0.541 | 39.925 | 44.360 | 43.376 | 40.630 | -2.746 |
| 7h final | 41.095 | 0.519 | 39.809 | 44.360 | 43.376 | 40.939 | -2.437 |

## 结论

本次只读长测成功完成。20 秒门槛测试正常，7 小时主测正常退出，2 h、4 h、6 h、7 h final 四个快照均已保存，并且每个快照都有原始 CSV、统计 JSON、10 分钟 bin CSV、全量图、Fz 图和 fxy 图。

数据上，Fz 的主要变化发生在前 2 小时：从 -43.25 N 变化到约 -40.74 N。之后 2 h 到 7 h final 的末值在 -40.37 N 到 -40.81 N 附近变化，整体比前 2 小时平缓。7 h final 的 Fz 首末变化为 +2.44 N，Fz 均值为 -40.967 N，std 为 0.519 N。

这里不能直接把 -40 N 当作“外力大小”下结论，因为传感器姿态、安装/放置状态、是否已由 OnRobot 侧做过历史 bias、以及传感器自身重力/线缆应力都会进入静态读数。这个实验更可靠的结论是长时间只读输出的漂移形态：前段有明显热/状态收敛，后段趋于较平。

## 下一步

下一次如果要把 OnRobot 作为 UR10e 外部力传感器使用，应先固定机械安装姿态和线缆走向，再做一次同样 7 小时静态 bench run 作为安装前基线。随后再做安装到 UR10e 后的 no-contact 静态 run，两份报告用同样的 2 h、4 h、6 h、final 口径比较 Fx/Fy/Fz、Tx/Ty/Tz 和合力模长。

## 附录

### A. 中间快照图片

2 h 快照：

![2 h full force/torque](checkpoints/2h/force_torque.png)

![2 h Fz](checkpoints/2h/fz.png)

![2 h Fx/Fy](checkpoints/2h/fxy.png)

4 h 快照：

![4 h full force/torque](checkpoints/4h/force_torque.png)

![4 h Fz](checkpoints/4h/fz.png)

![4 h Fx/Fy](checkpoints/4h/fxy.png)

6 h 快照：

![6 h full force/torque](checkpoints/6h/force_torque.png)

![6 h Fz](checkpoints/6h/fz.png)

![6 h Fx/Fy](checkpoints/6h/fxy.png)

### B. 复现实验命令

20 秒测试：

```bash
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s 20 \
  --out-dir measurements/archive/socketio_9hz_20260519_20260520/overnight_20260519_032648/test_20s \
  --status-every-s 5 \
  --flush-every-s 5
```

7 小时主测：

```bash
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s 25200 \
  --out-dir measurements/archive/socketio_9hz_20260519_20260520/overnight_20260519_032648/main_7h \
  --status-every-s 1800 \
  --flush-every-s 30
```

快照重分析：

```bash
python3 tools/analyze_onrobot_drift.py <checkpoint_dir> --baseline-s 600 --bin-s 600
```
