# Kunwei KWR75 当前进展报告（2026-06-08）

## 实验目的

这份报告把 Kunwei KWR75/KWR75B 当前证据单独整理出来，用于说明三件事：传感器与通信链路是否已经可用，长时间无运动 `1 kHz` 采集是否稳定，以及当前 Step2C 闭环直线实验走到什么程度。报告包含两层 OnRobot/Kunwei 对比：前 `600 s` 用于短窗口 noise/drift 判断，`6 h` 用于长时间漂移判断。两者都只作为 drift/noise 口径对照，不作为同机械状态下的绝对标定结论。

结论先给出：Kunwei TCP raw logging 已经支撑 `19 h 15 min`、约 `1 kHz`、无 parse error 的长跑；Step2C 已经完成 `search5 + guard20` 下的闭环直线，力均值能靠近 `-5 N`，但进入 line 阶段的瞬态和 Fz 波动仍是主要问题。机器人侧运动闭环频率不能写成 `500 Hz`，本轮 stage25 echo/motion gate 实测约 `246.55 Hz`。

## 设备与实验条件

| 项目 | 当前口径 |
|---|---|
| 传感器 | Kunwei KWR75/KWR75B 六轴力/力矩传感器 |
| 当前主采集路线 | Ubuntu TCP client -> `192.168.50.25:5152`，converted result stream |
| Ubuntu bench IP | `192.168.50.26/24` on `enp3s0` |
| Vendor GUI 状态 | Windows 11 原生 `SensorLinker.exe` 已验证 live 数据与 CSV 记录；Ubuntu/Wine 不是当前默认路线 |
| 长时采集状态 | 无机器人运动、无接触操作，只测传感器通信和静态读数 |
| Step2C zero 口径 | bridge 软件 baseline；未调用 Kunwei hardware tare，未调用 UR `zero_ftsensor()` |
| Step2C 参考线 | 长度约 `63.58 mm` 的 XY straight-line reference |
| 本报告图表口径 | 统计用选定窗口内全样本；长 trace 图用 min/max envelope，不用等间隔抽样线作为主证据 |
| 最长可用公共窗口 | Kunwei `19.26 h`，OnRobot UDP `8.79 h`；本报告长对比采用更适合汇报的 `6 h` |

## 实验命令

长时采集由 `capture_kunwei_kwr75_1khz.py` 运行，核心参数是 `--transport tcp-client --sensor-ip 192.168.50.25 --sensor-port 5152 --duration-s 86400 --checkpoint-interval-s 900`。Step2C 主 run 使用 `search5_guard20_line2ms_alpha70_vlim5` 版本，bridge 以 `--rtde-hz 500 --sensor-stale-s 0.10 --target-force-n 5 --max-normal-force-n 20` 运行。

本报告的生成脚本只读取已有 CSV/JSON 并写出报告资产，没有向 UR、OnRobot 或 Kunwei 发送命令，也没有做视频抽帧。

## 数据与图片

| artifact | 路径 |
|---|---|
| 本报告 summary | [assets/kunwei-kwr75-progress-20260608/analysis-summary.json](assets/kunwei-kwr75-progress-20260608/analysis-summary.json) |
| Kunwei 19h15min raw CSV | [../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/data.csv](../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/data.csv) |
| Kunwei 19h15min logger summary | [../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/summary.json](../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/summary.json) |
| Step2C metrics | [assets/step2c-kunwei-search5-guard20-line2ms/analysis-metrics.json](assets/step2c-kunwei-search5-guard20-line2ms/analysis-metrics.json) |
| OnRobot 600s UDP raw CSV | [../experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100/three_stream_600s_20260528_043052_onrobot_udp500_raw.csv](../experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100/three_stream_600s_20260528_043052_onrobot_udp500_raw.csv) |
| OnRobot 6h UDP raw CSV | [../experiments/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217/three_stream_24h_20260530_20260530_175220_onrobot_udp500_raw.csv](../experiments/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217/three_stream_24h_20260530_20260530_175220_onrobot_udp500_raw.csv) |

图 1 是本报告最主要的 OnRobot/Kunwei 前 `600 s` 对比图。两条曲线都先减去各自窗口第一帧，因此显示的是本窗口内的相对变化。阴影是每个时间 bin 内的 min/max envelope，实线是 bin mean；统计表仍使用窗口内所有样本。

![OnRobot vs Kunwei first 600s force axes](assets/kunwei-kwr75-progress-20260608/first600_onrobot_kunwei_force_axes_envelope.png)

图 2 单独展开 Fz。Kunwei 前 `600 s` 的 first-zeroed Fz 标准差是 `0.0113 N`，OnRobot UDP raw 是 `0.1541 N`。这个数值不能直接解释成传感器规格优劣，因为两个窗口的安装、载荷和日期不同。

![OnRobot vs Kunwei first 600s Fz](assets/kunwei-kwr75-progress-20260608/first600_onrobot_kunwei_fz_envelope.png)

图 3 把前三个力轴的 first-zeroed 标准差放在同一张图里，用于快速看 `600 s` 窗口内的波动量级。

![OnRobot vs Kunwei first 600s std](assets/kunwei-kwr75-progress-20260608/first600_onrobot_kunwei_force_std.png)

图 4 是本次新增的 `6 h` 长时间 Fz 对比。当前本地数据的最长公共窗口是 `8.79 h`，但本报告采用 `6 h` 作为主图口径，避免把会议汇报拖进过长的历史细节。统计仍使用 `6 h` 内全样本，图中阴影仍是 min/max envelope。

![OnRobot vs Kunwei 6h Fz](assets/kunwei-kwr75-progress-20260608/sixh_onrobot_kunwei_fz_envelope.png)

图 5 展示 `6 h` 的 Fx/Fy/Fz 三轴上下文。它用于判断 Fz 漂移是否伴随横向力变化。

![OnRobot vs Kunwei 6h force axes](assets/kunwei-kwr75-progress-20260608/sixh_onrobot_kunwei_force_axes_envelope.png)

图 6 是 `6 h` 窗口下三个力轴的 first-zeroed 标准差。

![OnRobot vs Kunwei 6h std](assets/kunwei-kwr75-progress-20260608/sixh_onrobot_kunwei_force_std.png)

## 统计结果

### Kunwei 19h15min 长时采集

| 指标 | 数值 |
|---|---:|
| 样本数 | `69,325,059` |
| first-to-last duration | `69325.773 s` |
| 平均采样率 | `999.989703 Hz` |
| parse errors | `0` |
| dropped sync bytes | `0` |
| Fz mean/std | `-0.8990 / 0.0146 N` |
| Fz last-first | `0.0457 N` |

这条结果支持把 Kunwei TCP route 作为当前 bench 的可用 `1 kHz` logging route。它仍不是完整 24h 结论，因为用户在约 `19 h 15 min` 主动停止。

### Step2C 闭环直线进展

| 项目 | 结果 |
|---|---:|
| 主 run | `bridge_step2c_search5_guard20_line2ms_alpha70_vlim5_20260606_220817` |
| 是否完成 line | `yes` |
| bridge stop reason | `duration` |
| bridge writes | `75,001` |
| RTDE output rate | `500.01 Hz` |
| raw sensor real-run rate | `1000.40 Hz` |
| stage25 echo rate | `246.55 Hz` |
| stage25 Fz mean/std | `-5.13 / 1.96 N` |
| stage25 error MAE | `1.57 N` |
| stage25 after 0.5s error MAE | `1.46 N` |
| raw stage25 Fz min | `-13.93 N` |
| XY error mean / p95 | `0.054 / 0.100 mm` |

这说明本轮主要瓶颈不是路径跟踪：XY error mean 约 `0.054 mm`，p95 约 `0.100 mm`。下一步应优先降低进入 line 阶段的力瞬态和稳态 Fz 波动。

### OnRobot vs Kunwei 前 600s

| Sensor | Axis | samples | duration (s) | rate (Hz) | zeroed mean (N) | zeroed std (N) | zeroed last-first (N) | raw min/max (N) |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Kunwei TCP raw | Fx | 599,988 | 599.971 | 1000.027 | -0.0034 | 0.0136 | -0.0389 | 2.327 / 2.529 |
| OnRobot UDP raw | Fx | 299,756 | 599.990 | 499.600 | -0.0119 | 0.0453 | -0.0400 | 0.060 / 0.470 |
| Kunwei TCP raw | Fy | 599,988 | 599.971 | 1000.027 | -0.0100 | 0.0122 | -0.0209 | -1.216 / -1.026 |
| OnRobot UDP raw | Fy | 299,756 | 599.990 | 499.600 | 0.0289 | 0.0302 | -0.0100 | 3.000 / 3.260 |
| Kunwei TCP raw | Fz | 599,988 | 599.971 | 1000.027 | 0.0043 | 0.0113 | 0.0219 | -1.604 / -0.497 |
| OnRobot UDP raw | Fz | 299,756 | 599.990 | 499.600 | 0.3025 | 0.1541 | 0.2200 | -25.900 / -24.500 |

| Sensor | Axis | zeroed std (Nm) | zeroed last-first (Nm) | raw min/max (Nm) |
|---|---|---:|---:|---|
| Kunwei TCP raw | Mx/Tx | 0.00033 | -0.00093 | 0.04669 / 0.05550 |
| OnRobot UDP raw | Mx/Tx | 0.00424 | -0.00299 | -0.00500 / 0.03200 |
| Kunwei TCP raw | My/Ty | 0.00033 | 0.00095 | -0.06751 / -0.06022 |
| OnRobot UDP raw | My/Ty | 0.00409 | 0.00000 | -0.10300 / -0.06700 |
| Kunwei TCP raw | Mz/Tz | 0.00033 | 0.00084 | 0.05950 / 0.06300 |
| OnRobot UDP raw | Mz/Tz | 0.00215 | 0.00300 | 0.06200 / 0.08100 |

比较限制必须写清楚：Kunwei 的前 `600 s` 来自 `19h15min` 未归零静态长跑，OnRobot 来自 `20260528` 的 dedicated `600s_first_zero` run；两者不是同一天、同治具、同预载的同步 A/B。这里能比较的是当前可用 raw stream 在自身 first-zero 口径下的短窗口稳定性和采样路线差异。

### OnRobot vs Kunwei 6h

本地可用数据里，Kunwei 最长为 `19.26 h`，OnRobot UDP raw 最长为 `8.79 h`，两者最长公共窗口为 `8.79 h`。本报告采用 `6 h` 作为长时间对比主口径；这个窗口已经足够覆盖慢漂移趋势，也更适合会议图表。

| Sensor | Axis | samples | duration (h) | rate (Hz) | zeroed std (N) | zeroed last-first (N) | back60-front60 mean (N) | raw min/max (N) |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Kunwei TCP raw | Fx | 21,599,781 | 6.000 | 999.990 | 0.0135 | -0.0086 | -0.0136 | 2.327 / 2.529 |
| OnRobot UDP raw | Fx | 10,791,425 | 6.000 | 499.603 | 0.0770 | 0.0000 | 0.0471 | -0.130 / 0.640 |
| Kunwei TCP raw | Fy | 21,599,781 | 6.000 | 999.990 | 0.0165 | 0.0264 | 0.0261 | -1.216 / -1.008 |
| OnRobot UDP raw | Fy | 10,791,425 | 6.000 | 499.603 | 0.0634 | 0.2300 | 0.2449 | 2.670 / 3.370 |
| Kunwei TCP raw | Fz | 21,599,781 | 6.000 | 999.990 | 0.0156 | 0.0176 | 0.0308 | -1.604 / -0.497 |
| OnRobot UDP raw | Fz | 10,791,425 | 6.000 | 499.603 | 0.3212 | 0.4000 | 0.7036 | -25.490 / -22.480 |

这个 `6 h` 对比仍然不是严格同治具同步 A/B。它更适合回答“当前两条 raw stream 的长窗口稳定性量级如何”，不适合回答“哪个传感器绝对零点更准”。

## 结论

1. Kunwei TCP raw logging 路线已经可用：`19 h 15 min` 内约 `1 kHz`，`parse_errors=0`，`dropped_sync_bytes=0`。
2. Kunwei 已经从传感器 bring-up 进入机器人闭环验证阶段。Step2C 主 run 能完成搜索、直线、卸载和回撤；均值层面能围绕 `-5 N` 工作。
3. 当前不能把 Step2C 写成机器人侧 `500 Hz` 闭环。bridge/RTDE logging 是 500Hz 级，但 URScript stage25 echo/motion gate 约 `246.55 Hz`。
4. OnRobot/Kunwei 前 `600 s` 与 `6 h` 对比图说明两条 raw stream 都可以做短窗口和长窗口漂移分析；但由于机械状态不同，报告只解释相对漂移和波动，不解释绝对偏置或规格优劣。

## 下一步

- Step2C 默认加入 settle stage，或先把 `normal velocity limit` 从 `±5 mm/s` 降到 `±3 mm/s`、`alpha` 从 `0.70` 降到 `0.50`，目标是降低 stage25 开头瞬态。
- 如果要正式做 OnRobot vs Kunwei A/B，应在同一机械状态、同一无接触窗口、明确 zero/tare 策略下同步或连续采集，不能把当前两个历史窗口当成严格标定对照。
- 如果目标是机器人侧 `500 Hz` 运动闭环，需要另开 `servoj/speedj`、多线程 URScript 或外部实时接口路线，而不是从当前 `speedl` echo 推断。

## 附录

### 报告生成命令

```bash
python3 /home/andy/ur10e_ros2_ws/weekly_meeting/build_kunwei_kwr75_report.py
```

### 生成口径

脚本对 `600 s` 窗口保留短窗口点列；对 `6 h` 窗口只保留统计量和时间 bin envelope，不把千万级样本全部留在内存里。统计直接使用窗口内所有样本；图形先按时间 bin 聚合为 min/max/mean envelope，保留尖峰范围，不使用等间隔抽样折线作为主要证据。HTML deck 只使用生成的数据图，没有抽取或嵌入新的视频帧。
