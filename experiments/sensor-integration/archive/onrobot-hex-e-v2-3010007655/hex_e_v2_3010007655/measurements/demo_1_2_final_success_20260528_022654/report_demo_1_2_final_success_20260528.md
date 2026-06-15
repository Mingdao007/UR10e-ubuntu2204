# UR10e / OnRobot demo-1.2 最终成功运行记录

## 实验目的

记录当前 `path_straight_line.urp` 成功运行时的力控参数、OnRobot 力数据和实际 TCP 路径。本文回答两个问题：

- `F/T Control` 在当前 PID 参数下是否维持了接触力；
- 实际 TCP 是否沿路径发生连续运动，以及报告能否给出 target-vs-actual 路径误差。

## 设备与实验条件

| 字段 | 数值 |
| --- | --- |
| 程序 | `path_straight_line.urp` |
| 数据 CSV | [demo_1_2_final_success_onrobot_vars_20260528_022724.csv](demo_1_2_final_success_onrobot_vars_20260528_022724.csv) |
| 控制器脚本快照 | [controller_snapshot/path_straight_line.script](controller_snapshot/path_straight_line.script) |
| 采样频率 | 请求 125 Hz，实际 125.000 Hz |
| 样本数 | 15001 |
| 采样时长 | 120.000 s |
| Safety mode | {'1': 15001} |
| Runtime state | {'1': 9529, '2': 5472} |
| Payload | 0.44 kg |
| TCP offset | `[0, 0, 0.12254, 0, 0, 0]` m/rad |

## 当前程序参数

| 参数 | 数值 | 证据 |
| --- | --- | --- |
| `F/T Search` 坐标系 | `frameID=1`，Tool | `path_straight_line.script` |
| Search 速度 | 0.010 m/s | `of_move_init` |
| Search waypoint | Tool Z = 50.0 mm | `of_waypoint(relativeP=...)` |
| Search 接触阈值 | `F3D=2.0 N` | `of_limit_start` |
| `F/T Control` 目标 | `Fz=5.0 N`，只开 Z compliant | `of_ft_control_start` |
| Force PID | `[0.5, 0.1, 0.1]` | `of_ft_control_start` |
| Torque PID | `[0.1, 0.0, 0.0]` | `of_ft_control_start` |
| `F/T Move` 速度 | 0.010 m/s | `of_move_init(frameID=0)` |
| `F/T Path` | `pathID=3964`, `relative=False` | `of_path_play` |
| retract | Tool Z = -20.0 mm | `retract_after_ft_path_tool_minus_z.script` |

## 数据与图片

图 1 显示 OnRobot `Fx/Fy/Fz` 全时序。图 2 单独放大 `Fz` 和 `|Fz|`，用于判断接触力是否围绕目标幅值。图 3 是实际 TCP 的 XY 轨迹，图 4 同时显示 TCP Z 和 TCP 速度。

![OnRobot Fx/Fy/Fz](figures/force_xyz_time.png)

![Fz 与目标线](figures/fz_time.png)

![TCP XY 路径](figures/tcp_xy_path.png)

![TCP Z 与速度](figures/tcp_z_speed_time.png)

![Runtime/Safety state](figures/runtime_safety_state.png)

## 统计结果

### 运行窗口

本次 120 s 采样内检测到 3 个 `runtime_state=2` 运行窗口。下表把三次运行按相同口径列出。

| Segment | Start (s) | End (s) | Active duration (s) | Active samples | Moving samples |
| --- | ---: | ---: | ---: | ---: | ---: |
| Run 1 | 29.984 | 44.680 | 14.696 | 1838 | 1718 |
| Run 2 | 54.272 | 68.616 | 14.344 | 1794 | 1711 |
| Run 3 | 85.024 | 99.736 | 14.712 | 1840 | 1720 |

### 力统计

总表统计窗口采用所有 `runtime_state=2` 且 TCP 线速度超过 1 mm/s 的样本。分 run 表采用相同口径。

| 量 | Mean | Std | Min | P1 | P50 | P99 | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fx (N) | -0.153 | 0.166 | -0.887 | -0.487 | -0.083 | 0.101 | 0.261 |
| Fy (N) | -0.090 | 0.104 | -0.421 | -0.311 | -0.056 | 0.096 | 0.306 |
| Fz (N) | -2.629 | 2.612 | -11.405 | -8.281 | -3.155 | 0.349 | 0.636 |
| Fxy (N) | 0.202 | 0.171 | 0.002 | 0.008 | 0.114 | 0.551 | 0.891 |
| F3D (N) | 2.706 | 2.546 | 0.006 | 0.030 | 3.156 | 8.290 | 11.409 |

| 指标 | 数值 |
| --- | ---: |
| `|Fz|` 相对目标幅值误差均值 | 2.660 N |
| `|Fz|` 相对目标幅值误差 P99 | 4.995 N |
| `|Fz|-target` 绝对误差 ≤ 1 N 比例 | 43.6 % |
| `|Fz|-target` 绝对误差 ≤ 2 N 比例 | 48.2 % |

| Segment | Fz Mean (N) | Fz Std (N) | Fz Min (N) | Fz P50 (N) | Fz Max (N) | `|Fz|-5` Mean (N) | `|Fz|-5` P99 (N) | ≤1 N (%) | ≤2 N (%) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Run 1 | -2.626 | 2.619 | -10.724 | -3.159 | 0.636 | 2.682 | 4.996 | 43.7 | 48.1 |
| Run 2 | -2.645 | 2.565 | -9.356 | -3.326 | 0.589 | 2.615 | 4.989 | 44.4 | 48.7 |
| Run 3 | -2.616 | 2.652 | -11.405 | -3.070 | 0.457 | 2.683 | 4.995 | 42.8 | 48.0 |

上表包含 approach、search、path、retract 中所有运动样本，因此会混入无接触阶段。下面的接触窗口表只统计 `|Fz| >= 2 N` 的样本，更适合判断 5 N 接触力是否跟上。

| Segment | Contact samples | Contact duration (s) | Fz Mean (N) | Fz Std (N) | Fz P50 (N) | Fz P99 (N) | `|Fz|-5` Mean (N) | `|Fz|-5` P99 (N) | ≤1 N (%) | ≤2 N (%) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Run 1 | 883 | 7.256 | -5.103 | 0.994 | -5.024 | -2.735 | 0.622 | 4.160 | 85.4 | 94.2 |
| Run 2 | 894 | 11.920 | -5.080 | 1.143 | -5.016 | -2.566 | 0.612 | 3.589 | 85.9 | 94.4 |
| Run 3 | 880 | 11.896 | -5.142 | 1.245 | -5.005 | -2.825 | 0.635 | 4.811 | 83.9 | 94.1 |

### 实际路径统计

| 指标 | 数值 |
| --- | ---: |
| 运动窗口起止 | 35.792 到 44.664 s |
| 运动窗口时长 | 8.872 s |
| 3D 路径长度 | 572.921 mm |
| XY 路径长度 | 477.285 mm |
| X 范围 | 444.605 到 489.290 mm |
| Y 范围 | 180.146 到 228.556 mm |
| Z 范围 | 19.335 到 39.975 mm |
| 最大 TCP 速度 | 41.148 mm/s |

| Segment | Moving duration (s) | 3D path (mm) | XY path (mm) | X range (mm) | Y range (mm) | Z range (mm) | Max speed (mm/s) |
| --- | ---: | ---: | ---: | --- | --- | --- | ---: |
| Run 1 | 13.984 | 190.774 | 159.151 | 444.605 到 489.230 | 180.159 到 228.529 | 19.335 到 39.918 | 41.148 |
| Run 2 | 13.992 | 191.167 | 159.394 | 444.639 到 489.289 | 180.226 到 228.529 | 19.344 到 39.941 | 40.947 |
| Run 3 | 13.976 | 190.801 | 158.636 | 444.626 到 489.290 | 180.146 到 228.556 | 19.344 到 39.975 | 40.822 |

## 结论

当前报告可以直接证明三次运行里实际 TCP 都发生了路径运动，并给出运动期间的 OnRobot 力统计。按 `|Fz| >= 2 N` 的接触窗口看，三次运行的 Fz 均值接近 -5 N，`|Fz|-5` 平均误差约 0.6 N，大部分接触样本在 1 N 误差内。是否“精确跟上 recorded F/T Path 的目标轨迹”，这份数据不能直接给出 target-vs-actual 误差，因为 `pathID=3964` 的目标路径采样没有从 OnRobot Compute Box 或 URScript 暴露出来。图 3 因此是实际轨迹审查，而不是目标轨迹误差图。

三次运行的 XY 路径长度分别约 159.2 mm、159.4 mm、158.6 mm，X/Y/Z 范围高度一致；按实际 TCP 轨迹看，路径执行是连续且可重复的。这次记录可以作为当前 PID `[0.5, 0.1, 0.1]` 下的成功 baseline。若需要正式证明 path tracking error，需要额外导出或重建目标路径采样，或者用相对 `F/T Waypoint` 替代 recorded `F/T Path`。

## 下一步

建议下一次 baseline 改用可审计的短相对 waypoint 路径，保留同样的 `Fz=+5 N` 和 PID 参数，再生成 target-vs-actual 路径误差。这样报告里可以从“实际路径看起来连续”升级到“路径误差多少 mm”。

## 附录

### 采集边界

Ubuntu 侧只读 RTDE：没有发送 URScript、没有写寄存器、没有发运动、没有 zero/bias/filter/speed/TCP/payload 写入。

### 关键文件

- 原始 CSV：[demo_1_2_final_success_onrobot_vars_20260528_022724.csv](demo_1_2_final_success_onrobot_vars_20260528_022724.csv)
- 分析摘要：[analysis/analysis_summary.json](analysis/analysis_summary.json)
- 控制器脚本快照：[controller_snapshot/path_straight_line.script](controller_snapshot/path_straight_line.script)
- 程序树：[controller_snapshot/path_straight_line.txt](controller_snapshot/path_straight_line.txt)
