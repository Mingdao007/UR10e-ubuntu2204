# Kunwei + UR 空夹爪自拟合报告

## 实验目的

用 Kunwei wrench + UR RTDE pose 对当前 RG2 打开、空夹爪状态做独立 gravity-axis 与 payload/CoG 拟合，并与已完成的 PolyScope 内置快照比较；本轮不写回控制器。

## 设备与实验条件

| 项目 | 记录 |
|---|---|
| Robot | UR10e, URSoftware 5.26.0.140462 |
| RG2 | 打开、空夹爪、无接触 |
| Kunwei | KWR75B TCP streaming, 1 kHz class |
| UR pose | RTDE observer, 静止姿态 |
| Zero/tare | 未执行 |
| PolyScope reference | 1.55 kg, CoG [1, 13, 51] mm |
| TCP readback | z=26.26 mm；与旧记录不一致，未用于替换 |
| Mass semantics | Kunwei sensing-plane candidate; not whole-assembly scale total |

## 实验命令

采集使用 `capture_kunwei_payload_cog_calibration.py`；分析优先读取同一 run 的 `summary.json`，原始 CSV/原始 Kunwei 帧保持不变。

## 数据与图片

- 原始 CSV：`/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260823_dual_ft_unfilled_box_current_w3_post_wizard/gravity_axis_calibration.csv`
- 原始 Kunwei frames：`/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260823_dual_ft_unfilled_box_current_w3_post_wizard/kunwei_raw_frames.bin`
- 主拟合图：![force/moment fit](custom_payload_cog_fit.png)

## 统计结果

| Segment | Samples | Contact | Zeroed | Purpose |
|---|---:|---|---|---|
| P0 | 8049 | no contact | no | static gravity pose |
| P1 | 8000 | no contact | no | static gravity pose |
| P2 | 8052 | no contact | no | static gravity pose |
| P3 | 8000 | no contact | no | static gravity pose |
| P0_return_repeat_drift_validation | 8035 | no contact | no | static gravity pose |

| Method | Mass (kg) | CoG frame | CoG (mm) | Force RMSE (N) | Moment RMSE (Nm) | Status |
|---|---:|---|---|---:|---:|---|
| PolyScope built-in | 1.5500 | UR tool | [1, 13, 51] | N/A | N/A | reference snapshot |
| Kunwei custom | 1.1681094382990838 | Kunwei sensor | [0.1515145830551325, 0.5603083100098105, 76.48079104194676] | 0.0500362332030612 | 0.0011036812006844374 | fitted |

- mapping：`{'rmse_n': 0.05003623320306092, 'corr': 0.9046639557374919, 'alpha_kg': 1.1681094382990846, 'weight_n': 11.455240423095718, 'mass_kg': 1.1681094382990846, 'bias_n': [8.054077261003002, 3.151212144105573, 0.8145023862659753], 'permutation': [0, 1, 2], 'signs': [1, 1, 1], 'formula': ['+F_K[0]', '+F_K[1]', '+F_K[2]']}`
- gravity rank：`3`；`max |g_T,z|=6.040299465395409` m/s²
- P0 return force drift norm：`None` N
- P0 return moment drift norm：`None` Nm

## 结论

Kunwei sensing-plane 自拟合质量候选为 `1.1681094382990838` kg，比内置 `1.55 kg` 低约 `-25.59812494910295`%；mapping 的最佳轴排列为当前同轴正号，force RMSE 约 `0.05003623320306092` N。该数值不能直接代表整套拆下称重的总质量。
CoG 当前只得到 Kunwei sensor origin frame 的结果；由于 sensor-origin 到 UR tool frame 的机械变换尚未确认，不能把它与 PolyScope 的 [1,13,51] mm 做直接坐标差，也不应据此写回配置。

## 下一步

测量并确认 Kunwei reference origin 到 UR tool frame 的平移/旋转，再用同一 raw run 重算 tool-frame CoG；在此之前保持 PolyScope 的 1.55 kg / [1,13,51] mm 不变。

## 附录

- JSON：`/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260823_dual_ft_unfilled_box_current_w3_post_wizard/custom_payload_cog_analysis.json`
- Metadata：`/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260823_dual_ft_unfilled_box_current_w3_post_wizard/metadata.json`
- Summary：`/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260823_dual_ft_unfilled_box_current_w3_post_wizard/summary.json`

本报告为 diagnostic calibration record，不是 UR payload/TCP promotion certificate。
