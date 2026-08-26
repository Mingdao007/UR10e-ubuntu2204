# Historical snapshot notice

This report records the pre-wizard `1.55 kg / [1,13,51] mm` comparison and is
retained for provenance only. It is superseded by the current calibration
report: [kunwei-rg2-payload-calibration-20260821.md](kunwei-rg2-payload-calibration-20260821.md),
which records the live `1.33 kg / [-2,-6,65] mm` read-back.

# UR10e Kunwei + RG2 空夹爪自拟合对比

## 实验目的

独立拟合当前空 RG2 的 gravity-axis mapping、payload 和 CoG，并与 PolyScope 内置快照对比；本轮不写回控制器。

## 设备与实验条件

| 项目 | 记录 |
|---|---|
| Robot | UR10e, URSoftware 5.26.0.140462 |
| EOAT | RG2 打开、空夹爪、无接触 |
| Data source | Kunwei wrench + UR RTDE pose |
| Zero/tare | 未执行 |
| Built-in reference | 1.55 kg, CoG [1, 13, 51] mm |
| Current TCP readback | z=26.26 mm；与旧记录有 discrepancy，未覆盖 |
| Mass semantics | Kunwei sensing-plane candidate; not whole-assembly scale total |

## 数据与图片

- [raw CSV](../experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260820_192100_rg2_open_custom_compare/gravity_axis_calibration.csv)
- [Kunwei raw frames](../experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260820_192100_rg2_open_custom_compare/kunwei_raw_frames.bin)
- [capture summary](../experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260820_192100_rg2_open_custom_compare/summary.json)
- [analysis JSON](../experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260820_192100_rg2_open_custom_compare/custom_payload_cog_analysis.json)
- ![Kunwei force/moment fit](../experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260820_192100_rg2_open_custom_compare/custom_payload_cog_fit.png)

## 统计结果

| Segment | Samples | Contact | Zeroed | Purpose |
|---|---:|---|---|---|
| P0_initial | 4998 | no contact | no | static gravity pose |
| P0_return | 5035 | no contact | no | static gravity pose |
| T1_tilt_plus | 5000 | no contact | no | static gravity pose |
| T2_tilt_minus | 5000 | no contact | no | static gravity pose |
| T2_tilt_plus | 5000 | no contact | no | static gravity pose |
| W3_plus_180 | 5000 | no contact | no | static gravity pose |
| W3_plus_270 | 5036 | no contact | no | static gravity pose |
| W3_plus_90 | 5046 | no contact | no | static gravity pose |

| Method | Mass (kg) | CoG frame | CoG (mm) | Force RMSE (N) | Moment RMSE (Nm) | Status |
|---|---:|---|---|---:|---:|---|
| PolyScope built-in | 1.5500 | UR tool | [1, 13, 51] | N/A | N/A | reference snapshot |
| Kunwei custom | 1.0504405462464208 | Kunwei sensor | [0.35222305409425153, 0.34816338930613944, 59.14058970424245] | 0.09681035613265365 | 0.0021154765885357915 | fitted |

- payload delta：`-0.49955945375357924` kg（`-32.22964217765028`%）
- axis mapping：`{'rmse_n': 0.09681035613265354, 'corr': 0.8758212970757474, 'alpha_kg': 1.0504405462464212, 'weight_n': 10.301302782847467, 'mass_kg': 1.0504405462464212, 'bias_n': [7.661211981926198, 3.0042916708259257, 0.6860922253426467], 'permutation': [0, 1, 2], 'signs': [1, 1, 1], 'formula': ['+F_K[0]', '+F_K[1]', '+F_K[2]']}`
- gravity rank：`3`；P0 return drift：`{'force_n': [-0.03134855374889689, -0.07434626581593573, 0.04958790053341211], 'moment_nm': [-0.0002742120192900932, 0.0015817525742765626, -0.0036320220470665854]}`

## 结论

本次 Kunwei sensing-plane 自拟合质量候选为 `1.0504405462464208` kg，与内置 `1.55 kg` 不一致（差 `-0.49955945375357924` kg）。mapping 结果为当前同轴正号，且姿态集合已达到三维重力方向 rank。它不能直接与整套硬件秤重总质量比较。
CoG 只在 Kunwei sensor origin frame 中得到；sensor-origin 到 UR tool frame 的机械变换尚未确认，因此 CoG 对比状态为 `inconclusive`，不执行 payload/CoG/TCP 写回。

## 下一步

确认 sensor-origin 到 UR tool frame 的机械变换后，重用本次 raw run 重算 tool-frame CoG；在此之前保持内置值不变。

## 附录

- 采集目录：`/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260820_192100_rg2_open_custom_compare`
- 原始 CSV：`/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/measurements/gravity_axis_calibration/20260820_192100_rg2_open_custom_compare/gravity_axis_calibration.csv`
- 分析命令：`analyze_custom_payload_cog.py <run_dir> --builtin-payload-kg 1.55 --builtin-cog-mm 1 13 51`
