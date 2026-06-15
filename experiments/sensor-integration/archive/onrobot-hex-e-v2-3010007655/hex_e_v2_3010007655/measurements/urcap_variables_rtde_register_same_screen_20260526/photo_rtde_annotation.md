# PolyScope Variables 图片标注与 RTDE 对照

日期：2026-05-26

## 正确图片

目标图片不是最新的 PhotoSync 设置截图，而是已经归档的 Variables 面板图：

`/Users/andyl/Documents/UR10e/ft_sensor/incoming_to_classify/20260526_urcap_rtde_variable_export_panel_20260526/IMG_1443.JPG`

本地预览：

`/home/andy/.cache/codex/phone-photo-intake/previews/IMG_1443_902826b95a0c.jpg`

这张图显示 PolyScope `Variables` 面板，里面有 `Fx/Fy/Fz/Tx/Ty/Tz` 和
`tFT`。另一张 `IMG_1442.JPG` 是 Script Code/program 编辑界面，用来判断底部小箭头 UI，不是变量读数图。

## 图片读数标注

| 图中变量 | 读数 |
|---|---:|
| `F3D` | `0.17209` |
| `Fx` | `0.04561` |
| `Fy` | `0.08336` |
| `Fz` | `-0.14347` |
| `T3D` | `0.00639` |
| `Tx` | `-0.00327` |
| `Ty` | `0.00519` |
| `Tz` | `-0.00178` |
| `tFT` | `[0.04561, 0.08336, -0.14347, -0.00327, 0.00519, -0.00178]` |

## RTDE 同屏采样窗口

RTDE 文件：

- Summary:
  `/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/urcap_variables_rtde_register_same_screen_20260526/urcap_ft_rtde_registers_same_screen_20260526_222943_summary.json`
- CSV:
  `/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/urcap_variables_rtde_register_same_screen_20260526/urcap_ft_rtde_registers_same_screen_20260526_222943.csv`

RTDE 采样窗口：

- 开始：`2026-05-26T22:29:43`
- 结束：`2026-05-26T22:29:53`
- 样本数：`1172`
- 采样率：`125.002 Hz`

## 图片读数与 RTDE 对照

这张图片的文件时间是 `2026-05-26T22:19:53+08:00`，RTDE 同屏采样窗口是
`22:29:43` 到 `22:29:53`。因此下表只能说明“同一口径数量级一致”，不能作为严格同一秒读数验证。

| 通道 | 图片读数 | RTDE 10 s 均值 | 图片 - 均值 | RTDE 最后样本 | 图片 - 最后样本 |
|---|---:|---:|---:|---:|---:|
| `Fx` / `fx_n` | `0.04561 N` | `0.00460 N` | `0.04101 N` | `-0.00315 N` | `0.04876 N` |
| `Fy` / `fy_n` | `0.08336 N` | `-0.01058 N` | `0.09394 N` | `0.02385 N` | `0.05951 N` |
| `Fz` / `fz_n` | `-0.14347 N` | `0.03839 N` | `-0.18186 N` | `-0.11522 N` | `-0.02825 N` |
| `Tx` / `tx_nm` | `-0.00327 Nm` | `0.00131 Nm` | `-0.00458 Nm` | `0.00286 Nm` | `-0.00613 Nm` |
| `Ty` / `ty_nm` | `0.00519 Nm` | `0.00128 Nm` | `0.00391 Nm` | `0.00364 Nm` | `0.00155 Nm` |
| `Tz` / `tz_nm` | `-0.00178 Nm` | `-0.00094 Nm` | `-0.00084 Nm` | `-0.00182 Nm` | `0.00004 Nm` |

## 判断

1. `IMG_1443.JPG` 的 Variables 面板读数已经完整读出并标注。
2. 图中 `Fz=-0.14347 N` 和 10 s RTDE 窗口的 `Fz` 最后样本
   `-0.11522 N` 接近，但时间戳不同，不能当成严格同一时刻验证。
3. 要完成严格闭环，需要在 RTDE 采样窗口内拍到 Variables 面板，或者由用户在
   `开始同屏采样` 后 10 秒内直接报出面板数值。
