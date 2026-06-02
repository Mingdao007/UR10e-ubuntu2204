# V1 Simulation Start Report

日期：2026-05-23  
范围：按 `REPRODUCTION_PLAN.md` 的 v1 口径，先启动仿真；不执行真实 UR10e、URCap、zero/bias/filter/TCP/payload 或任何硬件动作。

## 审核结论

- 实验目录原始状态只有 `REPRODUCTION_PLAN.md`，缺少 `configs/`、`src/`、`scripts/`、`assets/`、`runs/`。
- 系统 Python 原本缺 `mujoco`。
- `python3 -m venv` 失败，因为当前 Ubuntu 缺 `python3.10-venv` / `ensurepip`。
- 已改用用户级 Python site-packages 安装仿真依赖。
- ROS 2 xacro 可用，`/opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro` 存在。

## 已创建的仿真资产

- `configs/paper_truth.yaml`
- `configs/mujoco_ur10e.yaml`
- `assets/urdf/ur10e_nominal.urdf`
- `assets/mjcf/ur10e_nominal.xml`
- `src/tase_repro/finite_time.py`
- `scripts/run_fig5_r_sweep.py`
- `scripts/run_ur10e_mujoco_adaptation.py`

`assets/mjcf/ur10e_nominal.xml` 是 v1 approximate model，不是 calibrated UR10e，不含真实 OnRobot/EOAT CAD。TCP 使用 85 mm `unverified_tcp_guess`。

## 运行记录

安装依赖：

```bash
python3 -m pip install --user mujoco qpsolvers osqp
```

确认版本：

```text
mujoco 3.8.1
qpsolvers 4.12.0
osqp 1.1.1
numpy 1.21.5
```

Fig.5 r sweep：

```bash
python3 scripts/run_fig5_r_sweep.py --config configs/paper_truth.yaml
```

输出目录：

```text
runs/fig5_r_sweep/20260523T113301
```

UR10e MuJoCo smoke：

```bash
python3 scripts/run_ur10e_mujoco_adaptation.py --config configs/mujoco_ur10e.yaml --smoke
```

最终采用的有效输出目录：

```text
runs/ur10e_smoke/20260523T113420
```

中间 smoke run `20260523T113313` 无接触，`20260523T113341` 接触力过大；已通过下调初始高度修正到最终 run。

## 结果摘要

Fig.5 r sweep 收敛时间：

| r | convergence_time_s |
| --- | ---: |
| 0.2 | 0.2490 |
| 0.4 | 0.3275 |
| 0.6 | 0.4675 |
| 0.8 | 0.7475 |
| 1.0 | 1.3800 |

UR10e smoke:

| 指标 | 值 |
| --- | ---: |
| nq / nv / nu | 6 / 6 / 6 |
| duration_s | 2.0 |
| steps | 1000 |
| max_contact_count | 1 |
| mean_contact_count | 1.0 |
| max_contact_normal_force_N | 5.8794 |
| final_tcp_z_m | 0.04485 |

## 未覆盖项

- Fig.5 使用 `pending_pdf_verify` 草值，仅 warning，不是 PDF-verified result。
- UR10e smoke 是静态接触与模型加载验证，不是 QP controller、force ladder 或轨迹跟踪。
- MJCF 是近似串联链，不是从真实 calibrated UR10e 转换出的精确模型。
- `ur10e_calibration.yaml` 仍待实机生成。
- EOAT/TCP 仍待实测；85 mm 不得用于真实运动。

