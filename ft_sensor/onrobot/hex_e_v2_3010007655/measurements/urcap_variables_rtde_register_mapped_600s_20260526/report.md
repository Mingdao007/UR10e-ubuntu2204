# OnRobot URCap Variables RTDE 映射 600 s 实验报告

日期：2026-05-26

## 实验目的

本实验验证一个具体判断：Ubuntu 能否通过 UR RTDE 长时间稳定记录 PolyScope OnRobot Variables 面板里的 `Fx/Fy/Fz/Tx/Ty/Tz` 近零口径，而不是直接 Compute Box TCP/UDP raw 口径。

结论先行：这条 no-motion 映射链路在 600 s 内可用。RTDE logger 无错误，采样率为 `124.999 Hz`，`Fz` 全程均值为 `-0.0256 N`。这与此前 direct TCP/UDP raw `Fz` 约 `-28` 到 `-32 N` 的口径明显不同。

## 设备与实验条件

| 项目 | 内容 |
|---|---|
| 机器人 | UR10e，Dashboard/RTDE host `192.168.1.18` |
| 传感器链路 | OnRobot HEX-E v2 / FT-OnRobot URCap variables |
| Ubuntu 读取路径 | UR RTDE `output_double_register_24..29` |
| PolyScope 映射 | no-motion Script 将 `Fx/Fy/Fz/Tx/Ty/Tz` 写入 `output_double_register_24..29` |
| 采样频率 | 请求 `125 Hz`；实测 `124.999 Hz` |
| 采样时长 | 请求 `600 s`；首末样本跨度 `599.307836 s` |
| 样本数 | `74,914` |
| 接触/运动状态 | 未发送运动命令；本次实验只验证静态 no-motion 导出链路 |
| zero/bias/filter/speed | Ubuntu 未执行 `zero`、bias、filter、speed 命令；未改 TCP/payload |
| Dashboard 状态 | 采样期间和收尾读回均为 `Program running: true`、`PLAYING <unnamed>`、`Robotmode: RUNNING`、`Safetymode: NORMAL` |
| 可比口径 | 本报告只讨论 URCap/PolyScope Variables 经 RTDE register 导出的口径；不把 direct TCP/UDP raw 值放入同表比较 |

## 数据与图片

主要数据文件：

- [CSV](urcap_ft_rtde_registers_mapped_600s_20260526_215305.csv)
- [Summary JSON](urcap_ft_rtde_registers_mapped_600s_20260526_215305_summary.json)
- [分析指标 JSON](report_analysis_metrics.json)

图 1 显示 600 s 内三轴力和三轴矩的全量上下文。力信号保持在近零小范围内，矩信号也没有出现大幅阶跃。

![全量力/矩上下文](plots/force_torque_full_context.png)

图 2 单独放大 `Fz`。`Fz` 全程均值为 `-0.0256 N`，主要波动在亚牛顿范围内；前 60 s 到后 60 s 的均值变化为 `-0.1017 N`。

![Fz 单轴](plots/fz_only.png)

图 3 给出 `Fx` 和 `Fy` 双曲线。这里的 `fxy` 按 report-skill 定义表示 `Fx`、`Fy` 两条曲线，不是水平力模长。

![Fx/Fy 双曲线](plots/fxy_fx_fy.png)

## 统计结果

### 采样质量

| 指标 | 数值 |
|---|---:|
| RTDE logger 错误 | `0` |
| 样本数 | `74,914` |
| CSV 行数（含表头） | `74,915` |
| 实测采样率 | `124.999200 Hz` |
| 平均采样间隔 | `8.000051 ms` |
| p95 采样间隔 | `8.195788 ms` |
| p99 采样间隔 | `8.349391 ms` |

### 全程轴向统计

| 通道 | Mean | Std | Min | P1 | P50 | P99 | Max | `|x|>0.5` | `|x|>1.0` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `fx_n` | `0.026101 N` | `0.042386 N` | `-0.174395 N` | `-0.074395 N` | `0.025605 N` | `0.125605 N` | `0.215605 N` | `0` | `0` |
| `fy_n` | `0.021429 N` | `0.033907 N` | `-0.106641 N` | `-0.056641 N` | `0.023360 N` | `0.093359 N` | `0.163359 N` | `0` | `0` |
| `fz_n` | `-0.025583 N` | `0.145626 N` | `-0.663475 N` | `-0.363476 N` | `-0.023476 N` | `0.316525 N` | `0.646524 N` | `65` | `0` |
| `tx_nm` | `-0.005030 Nm` | `0.004172 Nm` | `-0.022273 Nm` | `-0.014273 Nm` | `-0.005273 Nm` | `0.004727 Nm` | `0.013727 Nm` | `0` | `0` |
| `ty_nm` | `-0.005100 Nm` | `0.003865 Nm` | `-0.022809 Nm` | `-0.013809 Nm` | `-0.004809 Nm` | `0.004191 Nm` | `0.011191 Nm` | `0` | `0` |
| `tz_nm` | `0.002890 Nm` | `0.002089 Nm` | `-0.005783 Nm` | `-0.001783 Nm` | `0.003217 Nm` | `0.007217 Nm` | `0.011217 Nm` | `0` | `0` |

力模长统计：

| Mean | Std | Min | P1 | P50 | P99 | Max | `|F|>0.5 N` | `|F|>1.0 N` |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `0.138527 N` | `0.082209 N` | `0.006533 N` | `0.020835 N` | `0.120050 N` | `0.389097 N` | `0.670768 N` | `75` | `0` |

### 前 60 s 与后 60 s 均值变化

| 通道 | 前 60 s mean | 后 60 s mean | 后 60 s - 前 60 s |
|---|---:|---:|---:|
| `fx_n` | `0.035848 N` | `0.020542 N` | `-0.015305 N` |
| `fy_n` | `-0.005470 N` | `0.047361 N` | `0.052831 N` |
| `fz_n` | `0.012335 N` | `-0.089407 N` | `-0.101743 N` |
| `tx_nm` | `-0.004596 Nm` | `-0.006931 Nm` | `-0.002335 Nm` |
| `ty_nm` | `-0.004942 Nm` | `-0.004658 Nm` | `0.000284 Nm` |
| `tz_nm` | `0.002570 Nm` | `0.003326 Nm` | `0.000756 Nm` |

## 结论

1. `output_double_register_24..29` 的 RTDE 映射路径已通过 600 s 验证。Ubuntu 侧只读采样无错误，采样率稳定在 `125 Hz` 附近。
2. `Fz` 的全程均值为 `-0.0256 N`，前 60 s 与后 60 s 均值差为 `-0.1017 N`。这个结果支持“近零 PolyScope/URCap Variables 口径可以被 Ubuntu 记录”的判断。
3. 本报告不能证明 direct Compute Box TCP/UDP raw 值已经被补偿到同一口径。此前 raw 口径的 `Fz` 约 `-28` 到 `-32 N`，本次 RTDE register 口径的 `Fz` 近零，两者应继续分开标注。
4. 本报告还缺一个人工同屏确认：在 no-motion export 程序运行时，把 PolyScope Variables 面板的 `Fx/Fy/Fz/Tx/Ty/Tz` 与 CSV 近实时读数对照一次。完成这个确认后，这条路径才可以作为正式“PolyScope Variables Ubuntu logger”口径冻结。

## 下一步

1. 停止当前 no-motion RTDE 映射程序，避免它长期占用 Program running 状态。
2. 下一次运行时做一次人工对照：你读 PolyScope Variables 面板上的 `Fx/Fy/Fz/Tx/Ty/Tz`，我同时采 5-10 s RTDE register，确认数值同屏一致。
3. 若同屏一致，把这个路径升级为正式记录 SOP；后续漂移实验默认用 `urcap_variables_rtde_register_mapped_*` 目录命名，并明确标注为 URCap/PolyScope Variables 口径。

## 附录：复现命令

采集命令：

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-onrobot-hex/scripts/sample_urcap_ft_rtde_registers.py \
  --seconds 600 \
  --hz 125 \
  --output-dir /home/andy/ur10e_ros2_ws/ft_sensor/onrobot/hex_e_v2_3010007655/measurements/urcap_variables_rtde_register_mapped_600s_20260526 \
  --prefix urcap_ft_rtde_registers_mapped_600s
```

采集边界：

- Ubuntu 仅读 RTDE output registers。
- Ubuntu 未发送 URScript。
- Ubuntu 未发送运动、zero、bias、filter、speed、TCP、payload 写命令。

绘图与统计产物：

- `plots/force_torque_full_context.png`
- `plots/fz_only.png`
- `plots/fxy_fx_fy.png`
- `report_analysis_metrics.json`
