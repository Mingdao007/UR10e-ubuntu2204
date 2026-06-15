# OnRobot HEX-E 上电后前 100 分钟温漂只读记录

## 实验目的

本次实验观察 OnRobot HEX-E 在上电后前 100 分钟的只读输出温漂。实验按用户已完成断电重启后的新 power-cycle warm-up run 处理；整个过程只读取 Compute Box 的 `/version` 和 Socket.IO 数据流，不调用 bias、zero、autocalib、firmware 或配置接口。

## 设备与实验条件

| 项目 | 内容 |
|---|---|
| 传感器 | OnRobot HEX-E v2，live serial: `HEXEB806` |
| 文档/包名标识 | `3010007655` |
| Compute Box | `192.168.1.1`，`/version` 返回 `4.1.8` |
| Ubuntu 网口 | `enp3s0`，`192.168.1.10/24` |
| 运行目录 | `/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/warmup_100min_20260519_120845` |
| 主测时长 | 6000 s，约 100 min |
| 阶段快照 | 30 min、60 min、90 min、100 min final |
| 安全边界 | 只读采集；未调用 bias、zero、autocalib、firmware 或配置接口 |
| fxy 规则 | Fx 和 Fy 两条曲线放在同一张图 |

连通性门槛检查已保存到 [connectivity_gate.txt](connectivity_gate.txt)。主测由 `tools/onrobot_socketio_logger.py` 采集，checkpoint 由 `main_100min/raw_wrench.csv` 按 `t_s` 阈值截取后统一重分析。

## 主测 100 min 图片

主测完整 force/torque 图如下。上图为 Fx/Fy/Fz，下图为 Tx/Ty/Tz；横轴是采集开始后的秒数。

![100 min OnRobot HEX-E force/torque](main_100min/force_torque.png)

Fz 单独图用于直接观察竖直方向读数随 warm-up 的变化。

![100 min OnRobot HEX-E Fz](main_100min/fz.png)

fxy 图把 Fx 和 Fy 作为两条曲线放在同一张图里，用于观察水平两个分量是否同步漂移或各自偏移。

![100 min OnRobot HEX-E Fx/Fy](main_100min/fxy.png)

## 统一结果表

主测 `main_100min/summary.json` 显示：`duration_s=6000.031`，`status=0`，`authenticated=True`，`bias=False`，`plot_errors.full/fz/fxy` 均为 `None`。主测最后一个样本位于 `t_s=6000.03 s`；`100 min final` checkpoint 严格按 `t_s <= 6000` 截取，所以比主测少 1 行。

| 快照 | 时长 (s) | 样本数 | Serial | Status | Auth | Bias | reconnect/error | Fz首值 (N) | Fz末值 (N) | Fz变化 (N) | Fz均值 (N) | Fz std (N) |
|---|---:|---:|---|---:|---|---|---:|---:|---:|---:|---:|---:|
| 30 min | 1799.9 | 16159 | HEXEB806 | 0 | True | False | 60/59 | -40.790 | -40.450 | 0.340 | -40.557 | 0.244 |
| 60 min | 3599.9 | 32322 | HEXEB806 | 0 | True | False | 119/118 | -40.790 | -40.020 | 0.770 | -40.386 | 0.321 |
| 90 min | 5399.8 | 48487 | HEXEB806 | 0 | True | False | 178/177 | -40.790 | -39.850 | 0.940 | -40.313 | 0.312 |
| 100 min final | 5999.9 | 53873 | HEXEB806 | 0 | True | False | 198/197 | -40.790 | -40.110 | 0.680 | -40.310 | 0.304 |

完整轴统计如下，四个阶段使用同一字段和同一分析脚本。

| 快照 | 轴 | 均值 | Std | Min | Max | 首值 | 末值 | 末-首 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 30 min | Fx (N) | 0.3429 | 0.0776 | 0.0400 | 0.6100 | 0.3500 | 0.4900 | 0.1400 |
| 30 min | Fy (N) | 3.2247 | 0.0524 | 3.0400 | 3.4200 | 3.2600 | 3.2800 | 0.0200 |
| 30 min | Fz (N) | -40.5572 | 0.2440 | -41.5700 | -39.6700 | -40.7900 | -40.4500 | 0.3400 |
| 30 min | Tx (Nm) | -0.1415 | 0.0072 | -0.1690 | -0.1110 | -0.1510 | -0.1540 | -0.0030 |
| 30 min | Ty (Nm) | 0.0087 | 0.0072 | -0.0200 | 0.0370 | 0.0010 | 0.0000 | -0.0010 |
| 30 min | Tz (Nm) | 0.0724 | 0.0038 | 0.0570 | 0.0870 | 0.0770 | 0.0770 | 0.0000 |
| 60 min | Fx (N) | 0.3790 | 0.0847 | 0.0400 | 0.7300 | 0.3500 | 0.5400 | 0.1900 |
| 60 min | Fy (N) | 3.2415 | 0.0527 | 3.0400 | 3.4400 | 3.2600 | 3.2300 | -0.0300 |
| 60 min | Fz (N) | -40.3862 | 0.3209 | -41.5700 | -39.2300 | -40.7900 | -40.0200 | 0.7700 |
| 60 min | Tx (Nm) | -0.1460 | 0.0087 | -0.1760 | -0.1110 | -0.1510 | -0.1590 | -0.0080 |
| 60 min | Ty (Nm) | 0.0044 | 0.0088 | -0.0280 | 0.0370 | 0.0010 | -0.0090 | -0.0100 |
| 60 min | Tz (Nm) | 0.0748 | 0.0046 | 0.0570 | 0.0920 | 0.0770 | 0.0800 | 0.0030 |
| 90 min | Fx (N) | 0.3911 | 0.0820 | 0.0400 | 0.7300 | 0.3500 | 0.3500 | 0.0000 |
| 90 min | Fy (N) | 3.2477 | 0.0510 | 3.0300 | 3.4400 | 3.2600 | 3.2300 | -0.0300 |
| 90 min | Fz (N) | -40.3132 | 0.3117 | -41.5700 | -39.2300 | -40.7900 | -39.8500 | 0.9400 |
| 90 min | Tx (Nm) | -0.1470 | 0.0081 | -0.1760 | -0.1110 | -0.1510 | -0.1530 | -0.0020 |
| 90 min | Ty (Nm) | 0.0026 | 0.0085 | -0.0280 | 0.0370 | 0.0010 | -0.0050 | -0.0060 |
| 90 min | Tz (Nm) | 0.0755 | 0.0044 | 0.0570 | 0.0920 | 0.0770 | 0.0800 | 0.0030 |
| 100 min final | Fx (N) | 0.3866 | 0.0819 | 0.0400 | 0.7300 | 0.3500 | 0.3400 | -0.0100 |
| 100 min final | Fy (N) | 3.2506 | 0.0512 | 3.0300 | 3.4400 | 3.2600 | 3.3000 | 0.0400 |
| 100 min final | Fz (N) | -40.3097 | 0.3043 | -41.5700 | -39.2300 | -40.7900 | -40.1100 | 0.6800 |
| 100 min final | Tx (Nm) | -0.1469 | 0.0079 | -0.1760 | -0.1110 | -0.1510 | -0.1480 | 0.0030 |
| 100 min final | Ty (Nm) | 0.0027 | 0.0083 | -0.0280 | 0.0370 | 0.0010 | 0.0030 | 0.0020 |
| 100 min final | Tz (Nm) | 0.0755 | 0.0043 | 0.0570 | 0.0920 | 0.0770 | 0.0770 | 0.0000 |

## 结论

本次 100 分钟只读 warm-up 主测完整完成，logger 正常退出，30/60/90/100 min final 四个 checkpoint 均已生成 `raw_wrench.csv`、`summary.json`、`drift_10min_bins.csv`、`force_torque.png`、`fz.png`、`fxy.png`。主测期间 `status=0`、`authenticated=True`、`bias=False`；记录到的 `poll_http_error` 与 `socket_handshake_ok` 成对出现，是 Socket.IO polling 会话周期性重建的记录，未导致主测失败。

从严格 `t_s <= 6000` 的 100 min final checkpoint 看，Fz 从 `-40.790 N` 到 `-40.110 N`，首末变化 `+0.680 N`。如果看主测最后一行，最后一个样本在 `t_s=6000.03 s`，Fz 为 `-39.830 N`，与 checkpoint 口径差异来自 6000 s 边界后的额外样本。

## Appendix A. 中间快照图片

30 min 快照：

![30 min full force/torque](checkpoints/30min/force_torque.png)

![30 min Fz](checkpoints/30min/fz.png)

![30 min Fx/Fy](checkpoints/30min/fxy.png)

60 min 快照：

![60 min full force/torque](checkpoints/60min/force_torque.png)

![60 min Fz](checkpoints/60min/fz.png)

![60 min Fx/Fy](checkpoints/60min/fxy.png)

90 min 快照：

![90 min full force/torque](checkpoints/90min/force_torque.png)

![90 min Fz](checkpoints/90min/fz.png)

![90 min Fx/Fy](checkpoints/90min/fxy.png)

## Appendix B. 完整命令和 checkpoint 逻辑

连通性门槛检查：

```bash
ip -br addr show enp3s0
ping -c 2 -W 1 192.168.1.1
curl -sS --max-time 3 http://192.168.1.1/version
```

100 分钟主测：

```bash
RUN_ROOT=/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/archive/socketio_9hz_20260519_20260520/warmup_100min_20260519_120845
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s 6000 \
  --out-dir "$RUN_ROOT/main_100min" \
  --status-every-s 600 \
  --flush-every-s 30
```

Checkpoint 截取规则：

```text
30min:        copy rows where float(t_s) <= 1800
60min:        copy rows where float(t_s) <= 3600
90min:        copy rows where float(t_s) <= 5400
100min_final: copy rows where float(t_s) <= 6000
```

每个 checkpoint 截取后运行同一分析命令：

```bash
python3 tools/analyze_onrobot_drift.py "$CHECKPOINT_DIR" --baseline-s 600 --bin-s 600
```

本次编排日志保存在 [orchestrator.log](orchestrator.log)。

## Appendix C. 阶段产物链接

| 阶段 | raw CSV | summary | 10 min bins | full 图 | Fz 图 | fxy 图 |
|---|---|---|---|---|---|---|
| main_100min | [raw_wrench.csv](main_100min/raw_wrench.csv) | [summary.json](main_100min/summary.json) | [drift_10min_bins.csv](main_100min/drift_10min_bins.csv) | [force_torque.png](main_100min/force_torque.png) | [fz.png](main_100min/fz.png) | [fxy.png](main_100min/fxy.png) |
| 30 min | [raw_wrench.csv](checkpoints/30min/raw_wrench.csv) | [summary.json](checkpoints/30min/summary.json) | [drift_10min_bins.csv](checkpoints/30min/drift_10min_bins.csv) | [force_torque.png](checkpoints/30min/force_torque.png) | [fz.png](checkpoints/30min/fz.png) | [fxy.png](checkpoints/30min/fxy.png) |
| 60 min | [raw_wrench.csv](checkpoints/60min/raw_wrench.csv) | [summary.json](checkpoints/60min/summary.json) | [drift_10min_bins.csv](checkpoints/60min/drift_10min_bins.csv) | [force_torque.png](checkpoints/60min/force_torque.png) | [fz.png](checkpoints/60min/fz.png) | [fxy.png](checkpoints/60min/fxy.png) |
| 90 min | [raw_wrench.csv](checkpoints/90min/raw_wrench.csv) | [summary.json](checkpoints/90min/summary.json) | [drift_10min_bins.csv](checkpoints/90min/drift_10min_bins.csv) | [force_torque.png](checkpoints/90min/force_torque.png) | [fz.png](checkpoints/90min/fz.png) | [fxy.png](checkpoints/90min/fxy.png) |
| 100 min final | [raw_wrench.csv](checkpoints/100min_final/raw_wrench.csv) | [summary.json](checkpoints/100min_final/summary.json) | [drift_10min_bins.csv](checkpoints/100min_final/drift_10min_bins.csv) | [force_torque.png](checkpoints/100min_final/force_torque.png) | [fz.png](checkpoints/100min_final/fz.png) | [fxy.png](checkpoints/100min_final/fxy.png) |
