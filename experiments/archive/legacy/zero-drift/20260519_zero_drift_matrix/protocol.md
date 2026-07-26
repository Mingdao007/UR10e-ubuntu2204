# UR10e Zero-Drift Matrix Protocol

本实验替代 2026-05-18 的不完整 S00/S01 初测。目标是获得同一口径的 UR10e 力传感器零漂矩阵：

| Segment | Zeroed? | Duration | Channels | 5s row source |
| --- | --- | ---: | --- | --- |
| S00_nozero_700s | No | 700 s | Fx/Fy/Fz/|F| | first 5 s of S00_nozero_700s |
| S01_rezero_700s | Yes | 700 s | Fx/Fy/Fz/|F| | first 5 s of S01_rezero_700s |

## 前置条件

- Robot Remote Control 已开启。
- Dashboard、Secondary Client、RTDE 可连接。
- Safety mode 为 `NORMAL`。
- 末端无接触、无运动、线缆无牵扯。
- 已恢复 fresh unzeroed 状态；如果之前执行过 `zero_ftsensor()`，需要先重启或用等价方式清掉控制器内的 force zero offset。

## 执行命令

先做只读 preflight：

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/run_zero_drift_matrix.py \
  --confirm-no-contact \
  --confirm-fresh-unzeroed \
  --seconds 700 --hz 20 \
  --output-dir /home/andy/ur10e_ros2_ws/experiments/archive/legacy/zero-drift/20260519_zero_drift_matrix \
  --preflight-only
```

确认 preflight 通过且初始 force norm 不像已清零状态后，运行完整矩阵：

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/run_zero_drift_matrix.py \
  --confirm-no-contact \
  --confirm-fresh-unzeroed \
  --seconds 700 --hz 20 \
  --output-dir /home/andy/ur10e_ros2_ws/experiments/archive/legacy/zero-drift/20260519_zero_drift_matrix
```

采集完成后分析：

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/analyze_zero_drift_matrix.py \
  --experiment-dir /home/andy/ur10e_ros2_ws/experiments/archive/legacy/zero-drift/20260519_zero_drift_matrix
```

## 报告规则

- 报告必须用中文。
- 5s 结果来自对应 700s trace 的起始 5s，不再单独混入 quick snapshot。
- Fx/Fy/Fz/|F| 的统计和图片必须同口径。
- raw-equivalent 曲线只能标为 derived，不能写成实测未清零 700s。
