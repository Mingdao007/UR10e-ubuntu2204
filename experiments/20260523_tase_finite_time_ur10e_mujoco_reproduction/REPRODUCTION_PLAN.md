# TASE finite-time force-motion control reproduction plan (UR10e adapted)

日期：2026-05-23（2026-05-23 审计后修订）  
目标：以 UR10e MuJoCo 为第一复现平台，先完成 UR10e-first adapted reproduction；保留论文参数、Fig.5 数学验证和实机安全门槛，7 自由度 paper-faithful 模型与 Fig.6 parity 后置。

审计交叉引用：`/home/andy/.claude/plans/implemented-the-plan-quirky-adleman.md`（条目 A1–A3、B1–B7、C polish 已在本文件中标注或修正）。

## 0.1. v1 范围与假设（不要被审计 gate 卡住）

本文件作为**第一版（v1）复现计划**。用户口径：v1 不需要特别精确，可以按理想情况设计；审计提出的 pending 项允许在 v1 阶段用 plausible 草值 + warning 推进，**只把实机安全相关的门槛当硬约束**（§9 / §10 不放松）。

v1 阶段默认假设（覆盖审计未闭合项）：

- **A1 策略**：默认走 UR10e-first 单线，paper-faithful 7DOF 进 deferred backlog。v2 重新审视时再决定是否恢复并行。
- **B1/B2/B3 paper truth**：先按 §4 / §4.1 草值跑，仿真脚本遇到 `pending_pdf_verify` 字段**输出 warning 但不 abort**；v2 启动 paper-truth-extraction 时再逐项核对 PDF。
- **B5 接触几何**：按 §A3.1 用 primitive 近似，单一接触 site，OnRobot/EOAT 不出 CAD。
- **接触表面（codex 第 3 点）**：v1 仅做单一材料解析平面，多材料切换、未知曲面进 v2 TODO；表面参数取 §A4 的 MuJoCo solref/solimp 默认值即可。
- **力源（codex 第 5 点）**：v1 仿真不绑定硬件力源；实机阶段若进入，默认 UR RTDE `actual_TCP_force` 500 Hz 作为控制环反馈，OnRobot HEX 仅外部记录。
- **TCP/EOAT（codex 第 4 点）**：v1 仿真使用 85 mm CAD guess，标 `unverified_tcp_guess`；实机运动必须先有实测，否则不进入 §10。
- **B4 URDF→MJCF**：手工 MJCF + `<include>` mesh 为默认；如果 v1 想快速过 smoke，允许临时换 `mujoco_menagerie` UR10e。

v2 TODO（不阻塞 v1，但写进 `reports/.../v2_followups.md`）：

1. paper-truth-extraction，关闭所有 `pending_pdf_verify`；
2. 论文实验 #1–#4 轨迹形状/材料/目标力的 PDF 核对（§4.1）；
3. orientation signal 维度歧义 resolution；
4. A1 策略正式拍板（UR10e-first vs paper-first‖code-first 并行）；
5. EOAT v13 真实 TCP / 质量 / 质心实测；
6. 接触面多材料件设计与边界标注；
7. OnRobot/EOAT 官方 CAD 或实测尺寸，替换 primitive 近似。

## 0. 结论先行

这篇论文的实验平台是 7 自由度 Franka，不是 UR10e。UR10e 是 6 自由度机械臂，不能把同一组冗余优化和同一组关节初值直接搬过去后仍称为“论文原平台复现”。

当前计划采用 UR10e-first 口径：

1. 近期主线：在 UR10e MuJoCo 模型上实现同一控制思想，明确标注为 adapted reproduction，重新处理 6 自由度非冗余情形、工具 TCP、接触面、力源和速度安全约束。
2. 保留论文真值合同：论文参数、控制律、Section V/VI 轨迹与 Fig.5 r sweep 仍作为算法对照。
3. 后置严格论文复现：7 自由度 Franka/FR3 或抽象 7 自由度 MuJoCo 模型、Fig.6 paper-style parity、paper-faithful MIAE 对照全部放入 deferred backlog。

在后置项完成前，报告不得把 UR10e 结果称为“论文原平台完整复现”；只能称为“UR10e 改编复现”。

## 1. 依据和本地事实

权威依据优先级：

1. 论文 PDF：`/home/andy/Zotero/storage/UZRF97KG/Xu 等 - 2026 - Finite-Time Convergence Neural Network-Based Force-Motion Control for Unknown Surface With Orientati.pdf`
2. 论文正文抽取出的 Section V / VI 参数和图指标。
3. 本地 RNN_F2 / tase_force_motion_sim 代码只作为参考，不作为真值。已有记忆明确说明 RNN_F2 不是最终可信实现，且存在 DH/自由度/平台不一致风险。
4. 旧会话 `019e4434-5e25-7132-974a-13f4d2b6ec77` 主要提供 UR10e + OnRobot 只读 bring-up 证据，不是 TASE 复现方案本身。

UR10e 本地事实：

- UR10e IP：`192.168.1.18`
- Ubuntu 控制网卡：`enp3s0`
- Ubuntu 侧地址：`192.168.1.10/24`
- ROS 2 workspace：`/home/andy/ur10e_ros2_ws`
- UR description：`/opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro`
- UR10e bench 配置：`/home/andy/ur10e_ros2_ws/src/ur10e_bringup/config/bench.yaml`
- UR10e calibration：**待生成**。`bench.yaml` 的 `kinematics_params_file: config/ur10e_calibration.yaml` 是占位（`calibration_hash: pending_fresh_ur10e_calibration`），实际文件目前不存在。仿真线先用 nominal DH；实机阶段必须先用 `ur_calibration` 从实机抓 calibration 写入此路径，再走 §A3 / §E1。
- 已知 RTDE `actual_TCP_force` 可按 500 Hz 记录，但曾在 POWER_OFF 工况全零，只能说明通道频率，不说明接触力有效性。
- OnRobot Web/Socket.IO 约 9 Hz，不适合作为高带宽控制反馈；OnRobot 高速路径需要单独验证，不能直接运行会 bias/改滤波的官方示例。
- EOAT v13 的 85 mm TCP 只是 CAD 候选值，不能直接当成已标定 UR TCP。

## 2. 工作目录

本复现新开目录：

```text
/home/andy/ur10e_ros2_ws/experiments/20260523_tase_finite_time_ur10e_mujoco_reproduction/
  REPRODUCTION_PLAN.md
  configs/
    paper_truth.yaml
    mujoco_ur10e.yaml
    hardware_gate.yaml
  assets/
    mjcf/
    urdf/
    surfaces/
  src/
    tase_repro/
      finite_time.py
      kinematics.py
      contact.py
      controller.py
      metrics.py
      plots.py
  scripts/
    setup_env.sh
    build_ur10e_mjcf.sh
    run_fig5_r_sweep.py
    run_ur10e_mujoco_adaptation.py
    run_hardware_readonly_check.py
  deferred/
    paper_faithful_7dof_notes.md
  tests/
    test_finite_time.py
    test_kinematics.py
    test_contact_sign.py
    test_orientation_error.py
    test_constraints.py
  runs/
  reports/
```

图与产物命名约定（在所有 `runs/<run>/` 目录内统一）：`<phase>_<metric>_<run-id>.png` / `.yaml` / `.npz`。例如 `fig5_r-sweep_20260524T1500.png`、`ur10e-smoke_contact-force_20260524T1530.png`、`ur10e-traj_cycloid_20260524T1600.npz`。

## 3. Phase A: MuJoCo 配置

### A1. Python 环境

建议使用实验目录内 venv，避免污染 ROS 2 系统 Python：

```bash
cd /home/andy/ur10e_ros2_ws/experiments/20260523_tase_finite_time_ur10e_mujoco_reproduction
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install mujoco numpy scipy matplotlib pyyaml lxml trimesh pytest
python -c "import mujoco; print(mujoco.__version__)"
```

当前机器已确认系统 Python 里没有 `mujoco`，所以这一步是第一道配置门槛。

### A2. Deferred: Paper-faithful 7DOF MuJoCo 模型

本阶段不执行 A2。不要在当前实现中先选择 Franka/FR3 或抽象 7 自由度模型，也不要把 `mujoco_paper7.yaml`、`run_fig6_paper_mujoco.py` 作为近期必需产物。

A2 的作用改为后置严格论文复现。只有当后续要恢复“论文原平台严格复现”时，才启动这部分。

后置模型选择：

- 首选：Franka/FR3 7 自由度 MuJoCo 模型，因为论文实验平台是 Franka 系列。
- 备选：抽象 7 自由度 serial chain，只要 DH/关节轴/末端任务空间可被完整定义，并且能严格跑通论文算法。

后置启动时必须记录：

- 模型来源和版本。
- 每个关节轴、关节限位、速度限位。
- 末端 frame 定义。
- 接触点 frame 和法向方向。
- Fig.6 paper-style 六子图和 paper-faithful MIAE 对照。

#### A2.1. Deferred 入口候选（不要从零开始）

`/home/andy/Documents/UR5e_ws/RNN_F2/` 已有 paper-first 脚手架。后置 A2 启动时必须先评估这些脚本能否复用，而不是另起一套：

- `forward_panda.m`、`getJacobian_panda.m`：Franka 7DOF 运动学候选；
- `run_paper_first.m`：「STP paper-first normal-only reference branch」；
- `run_paper_first_fullpose.m`：fullpose 变体；
- `run_paper_first_orientation_compare.m`：姿态分支比较；
- `run_paper_ideal.m` / `run_paper_ideal_fullpose.m` / `run_paper_ideal_fullpose_kkt.m`：paper-ideal 线；
- `run_paper_method_formula.m`、`run_paper_method_figure_match.m`、`run_paper_method_figure_bundle.m`：论文方法的 formula-faithful 与 figure-match 实现；
- `run_full_tase_reproduction.m`：串行编排 `formula_faithful_run` → `figure_match_run` → `formula_faithful_bundle` → `figure_match_bundle`；
- `plot_paper_first.m`、`plot_paper_ideal.m`、`plot_step_surface_force_paper_aligned.m`：paper-style 出图。

记忆 [[rnn-f2-code-not-final]] 仍然适用：这些脚本不是真值，但作为 paper-first 起点比从零搭建更省事。后置 A2 启动时的第一步是数值对照 PDF 而不是再写一份 Franka MJCF。

### A3. Current first model: UR10e MuJoCo 模型

UR10e 线使用 ROS 2 已安装的 UR description 生成 URDF，再转换或手工整理为 MJCF。

URDF 生成命令分两条分支：

**(a) 无 calibration 的 nominal DH（当前唯一可执行分支）**：

```bash
source /opt/ros/humble/setup.bash
xacro \
  /opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro \
  ur_type:=ur10e \
  name:=ur10e \
  > assets/urdf/ur10e_nominal.urdf
```

**(b) 含 calibration（实机阶段才可用）**：先用 `ros2 run ur_calibration calibration_correction --robot_ip 192.168.1.18 --output_parameters_file .../ur10e_calibration.yaml` 从实机抓取 calibration 写入 `bench.yaml` 指向的路径，再在 xacro 命令里加 `kinematics_params_file:=...ur10e_calibration.yaml`，输出 `ur10e_calibrated.urdf`。仿真阶段不需要这条分支。

URDF → MJCF 当前默认选**手工 MJCF + `<include>` UR mesh**（**defensible default, 用户可改**）：

- 不依赖第三方 `urdf2mjcf` 转换器（输出常常需要手工 patch 惯量/接触/sites）；
- 不直接用 `mujoco_menagerie` 的 UR10e（版本可能与 ROS UR description 漂移，且不含 OnRobot/EOAT）。
- UR description 的 mesh 是 `package://ur_description/meshes/...` URI，MuJoCo 不认 `package://`，先 `cp -r /opt/ros/humble/share/ur_description/meshes assets/mjcf/meshes/` 然后在 MJCF 用相对路径。
- 手工路径成本最高；如果用户偏好快速起步，可以改用 `mujoco_menagerie` 的 UR10e 作为初版，再在后续迭代里补 OnRobot/EOAT。需要换路径前在本节顶部记录决策。

完成 URDF/MJCF 后做三件事：

1. headless load：`python -c "import mujoco; m = mujoco.MjModel.from_xml_path('assets/mjcf/ur10e.xml')"` 必须无报错。
2. 手动检查惯量、joint axis、joint range、base frame、tool0 frame，与 ROS 2 UR description 一致。
3. 增加 OnRobot HEX / EOAT / 接触头几何体（落地路径见下）。85 mm TCP 只能作为仿真初值，文件中必须标注 `unverified_tcp_guess`。

#### A3.1. OnRobot HEX / EOAT 几何

当前 `experiments/onrobot_hex_e_v2_3010007655/eoat_design/` 没有公开 STL/STEP。落地路径选择**(b) 用 primitive 近似 + 单一接触点**：

- HEX-E v2：用 `<geom type="cylinder">` 近似机身（质量、质心从 OnRobot datasheet 取，注释来源）；
- EOAT v13：用 `<geom type="cylinder">` 加 `<geom type="sphere">` 近似 receiver + 接触头，质量来源在 `experiments/onrobot_hex_e_v2_3010007655/eoat_design/EOAT_TCP_NOTE.md`；
- 接触点：单个 `<site>` 放在 85 mm TCP guess 位置，所有接触力闭环只用这一个 site；
- 不建可视 mesh，等实机阶段拿到实测尺寸再细化。

如果之后拿到 OnRobot 官方 CAD，再切换到「(a) STL mesh」路径。

#### A3.2. UR10e 实机关节限位（与论文 7DOF 限位分开）

`§4` paper truth 的 `[-2.5, 2.5] rad` 是论文 7DOF 数字，只用于 paper-first 后置线。UR10e 实机限位是六关节、范围 ≈ ±2π（具体值以 UR description URDF 为准）。本 UR10e-first 主线在 `configs/mujoco_ur10e.yaml` 里单独列：

```yaml
ur10e_mujoco:
  joint_limit_rad: extracted_from_ur_description  # 后续脚本从 URDF 解析后写入
  joint_velocity_limit_rad_s: extracted_from_ur_description
```

QP 约束（见 §C4）只能用这两条 UR10e 的数值；论文 7DOF 数值在 UR10e 线里**不得**使用。

### A4. 接触环境

先从解析平面开始，后续再做未知曲面：

1. 平面：已知法向，验证力闭环符号和量纲。
2. 倾斜平面：验证由力反馈估计正交空间。
3. 曲面：验证未知表面和姿态顺应。
4. 多材料面：用于复现论文实验 #3/#4 的材料变化，但要放在 UR10e MuJoCo 通过之后。

接触模型必须显式记录：

- MuJoCo `solref` / `solimp`
- 接触刚度和阻尼等效值
- 摩擦参数
- 法向力正负号
- 传感器读数到控制器 `f` 的映射

## 4. Phase B: 论文真值合同

先创建 `configs/paper_truth.yaml`，不要把参数散落在脚本里。

> **PDF 核对门槛（v1 软门槛 / v2 硬门槛）**：以下所有数值是从既有材料初稿迁移过来的草值。在 `configs/paper_truth.yaml` 中以 `pending_pdf_verify: true` 标记未核对字段。
> - **v1**（当前阶段，按 §0.1 理想化设计）：仿真脚本遇到 `pending_pdf_verify` **输出 warning 后继续用草值跑**，不 abort。
> - **v2**（进入 paper-faithful 严格复现时）：必须开一轮 *paper-truth-extraction*，逐项标注 PDF（`/home/andy/Zotero/storage/UZRF97KG/Xu 等 - 2026 - Finite-Time Convergence ...pdf`）的 Section / 公式 / 表号；脚本遇到 `pending_pdf_verify` 必须 abort。

Section V 仿真真值（草值，待 PDF 核对）：

```yaml
paper:
  title: "Finite-Time Convergence Neural Network-Based Force-Motion Control for Unknown Surface With Orientation Compliance"
  section_v:
    q0: [0, -0.7853981634, 0, -2.3561944902, 0, 1.5707963268, 0.7853981634]  # pending_pdf_verify
    trajectory:
      x: "0.2*cos(0.2*t)"   # pending_pdf_verify
      y: "0.2*sin(0.2*t)"   # pending_pdf_verify
      z: "z0_TBD"           # pending_pdf_verify; z0 来源待定（接触面高度 vs fk(q0).z），与 §A4 接触面初始化绑定
    orientation_signal_raw: "[cos(0.1*t), sin(0.1*t)]"  # pending_pdf_verify
    orientation_signal_note: "论文这里与前文 3D orientation vector 存在维度歧义；paper-truth-extraction 必须产出一个明确的消歧义解释（哪两个分量、是否单位向量补齐），并写入本字段 resolution。"
    joint_limit_rad: [-2.5, 2.5]              # paper 7DOF only; UR10e 主线见 §A3.2
    joint_velocity_limit_rad_s: [-1.5, 1.5]   # paper 7DOF only; UR10e 主线见 §A3.2
    desired_normal_force_N: 5                 # pending_pdf_verify
    r_sweep: [0.2, 0.4, 0.6, 0.8, 1.0]        # pending_pdf_verify
```

Section VI 实验真值（草值，待 PDF 核对，仅用于后置 paper-faithful 线和 UR10e 改编对照基线）：

```yaml
paper:
  section_vi:
    Md_diag: 12        # pending_pdf_verify
    Bd_diag: 550       # pending_pdf_verify
    epsilon: 0.022     # pending_pdf_verify
    kp: 4              # pending_pdf_verify
    ko: 5              # pending_pdf_verify
    kf: 1              # pending_pdf_verify
    experimental_joint_limit_rad: [-3.0, 3.0]              # paper Franka only
    experimental_joint_velocity_limit_rad_s: [-0.15, 0.15] # paper Franka only
    force_filter: "none in paper"  # pending_pdf_verify
```

#### 4.1. 论文实验编号 → 轨迹映射（草值，pending PDF 核对）

下表是经验性映射，B2 必须在 paper-truth-extraction 一并闭合，对每条记录论文里的形状方程、表面材料、目标力、持续时间：

| 论文实验 | 当前草值轨迹 | pending |
| --- | --- | --- |
| #1 | cycloid | shape eq、surface、fd、duration |
| #2 | figure-eight | shape eq、surface、fd、duration |
| #3 | circle with material change | 材料切换点、fd、duration |
| #4 | cardioid with material change | 材料切换点、fd、duration |

如果 PDF 核对结果与上表不一致，§7 E3 与 §11 实验矩阵都必须同步更新。

近期指标合同：

- Fig.5：r sweep 的 finite-time 收敛曲线，必须输出每个 r 的收敛时间、稳态残差和是否出现初期振荡。
- UR10e MuJoCo：输出 force error、position error、orientation error、joint limit、velocity limit、controller saturation 和 contact stability。
- Fig.6：近期不做完整六子图 parity；只作为后置 paper-faithful 7DOF 复现的验收项。

通过条件：

- 所有图都有数据来源和单位。
- force error 不允许缺失。
- 关节限位和速度限位没有越界。
- 报告中清楚标注 UR10e adapted result，不把近期 UR10e 结果称为论文原平台复现。

## 5. Phase C: 算法实现顺序

### C1. Finite-time RNN 内环

先在无机器人模型的低维问题中复现 finite-time 收敛：

1. 实现 `sigr(x) = |x|^r sign(x)` 或论文等价形式。
2. 固定一个 QP/等式约束问题，验证 r = 0.2/0.4/0.6/0.8/1.0 的收敛曲线。
3. 保存 `runs/fig5_r_sweep/<timestamp>/metrics.yaml`。
4. 生成 Fig.5 风格图。

验收：

- r 越小通常收敛更快但更容易出现初期不平滑。
- r = 1 退化到普通指数/渐近风格，收敛慢于 finite-time 设置。
- 曲线和论文 Fig.5 的顺序一致。

### C2. 运动学和雅可比

实现并单元测试：

- 正运动学 `fk(q)`
- 几何雅可比 `J(q)`
- 数值差分雅可比对照
- 姿态误差定义
- 末端 twist 与 `J(q) qdot` 一致性

验收：

- 数值雅可比误差在明确容差内。
- 姿态误差在小角度下与解析近似一致。
- frame 命名以 UR10e 为当前主线；后置 paper-faithful 7DOF 线如恢复，必须使用单独 namespace，不混用。

### C3. 力/运动分解

先在已知法向平面验证：

- 法向方向 `n`
- 切向投影 `P_t = I - n n^T`
- 法向力误差 `ef = f - fd` 或 `fd - f` 的符号约定
- 切向轨迹跟踪
- 姿态顺应误差

验收：

- 增大接触压入量时，读数方向和控制器期望方向一致。
- 切向运动不会主动增加法向穿透。
- 法向力闭环能收敛到 `fd = 5 N`。

### C4. 约束优化

Deferred paper-faithful 7 自由度线：

- 当前不实现；后置恢复时保留论文中的冗余优化结构。
- 关节角约束：`[-2.5, 2.5] rad`
- 关节速度约束：`[-1.5, 1.5] rad/s`

当前 UR10e 6 自由度线：

- 不能照搬 7 自由度冗余 null-space 结论。
- 需要改写为约束最小二乘或 QP：
  - 主任务：满足可行的切向速度、法向力调节和姿态顺应。
  - 次任务：关节限位回避、速度限制、工具姿态限制。
  - 不可行时必须降级任务，而不是输出越界速度。

QP 后端选择：`qpsolvers` Python 包，主求解器 `osqp`（warm-start、稀疏、license-free），备选 `clarabel`。不使用 `cvxpy`（每步重建问题开销过大，不适合实时控制循环）。

降级策略（不可行时按优先级降）：

1. 法向力跟踪：硬约束，不放松；
2. 切向轨迹跟踪：用 slack 变量松弛，记录 slack 量级到 `metrics.yaml`；
3. 姿态顺应：可整体禁用，记录禁用窗口；
4. 关节限位与速度限位：硬约束，永不放松；
5. 输出阶段：如果求解器返回 infeasible，控制器输出 `qdot = 0` 并 raise `ControllerInfeasibleError`，由上层决定 stop/retry。

不允许在 QP 之外手工 clip qdot 然后假装求解成功——这条直接对应 §8 验收的「没有隐藏 saturation」。

## 6. Phase D: UR10e-first 仿真 bring-up

### D1. Fig.5 数学验证

脚本：

```bash
source .venv/bin/activate
python scripts/run_fig5_r_sweep.py --config configs/paper_truth.yaml
```

产物：

- `runs/fig5_r_sweep/<timestamp>/raw.npz`
- `runs/fig5_r_sweep/<timestamp>/metrics.yaml`
- `runs/fig5_r_sweep/<timestamp>/fig5_r_sweep.png`

完成标准：

- 每个 r 都有收敛时间。
- 收敛时间排序和论文趋势一致。
- r = 0.2 的初期不平滑或振荡如存在则记录，不强行抹平。

### D2. UR10e MuJoCo smoke

脚本：

```bash
source .venv/bin/activate
python scripts/run_ur10e_mujoco_adaptation.py --config configs/mujoco_ur10e.yaml --smoke
```

产物：

- `runs/ur10e_smoke/<timestamp>/raw_state.npz`
- `runs/ur10e_smoke/<timestamp>/raw_contact.npz`
- `runs/ur10e_smoke/<timestamp>/metrics.yaml`
- `runs/ur10e_smoke/<timestamp>/contact_force_check.png`
- `runs/ur10e_smoke/<timestamp>/tracking_error_check.png`

完成标准：

- UR10e MJCF 能 headless load。
- 关节顺序、joint range、base/tool/tcp frame 与 ROS 2 UR10e 描述一致。
- 接触平面法向力符号验证通过。
- 速度限制先用 `0.05 rad/s`，通过后再考虑 `0.15 rad/s`。
- force ladder 至少通过 `0.5 N` 和 `1.0 N`，再进入 `2.0 N` 和 `5.0 N`。
- 报告中写明这是 UR10e adapted smoke，不是 Fig.6 paper-style parity。

## 7. Phase E: UR10e MuJoCo 改编复现

### E1. 模型接入

输入（分支跟随 §A3）：

- 仿真主线：`assets/urdf/ur10e_nominal.urdf` + `assets/mjcf/ur10e_nominal.xml`；
- 实机线（calibration 已生成后）：`assets/urdf/ur10e_calibrated.urdf` + `assets/mjcf/ur10e_calibrated.xml`；
- OnRobot HEX / EOAT 几何：按 §A3.1 用 primitive 近似；
- 初始 TCP guess：85 mm，标注 `unverified_tcp_guess`，未实机验证。

检查：

- base frame 与 ROS 2 `base`/`base_link` 对齐关系。
- flange、tool0、tcp frame 分开记录。
- 关节顺序与 UR driver 一致。
- joint limits 取 §A3.2 的 UR10e 实机限位，**不得**使用 §4 paper truth 的 7DOF 数字。

### E2. UR10e 控制器改编

UR10e 只有 6 自由度，因此任务优先级要重新定义：

1. 法向力控制优先。
2. 接触面切向轨迹跟踪第二。
3. 姿态顺应第三，必要时降低姿态严格度。
4. 关节限位和速度限位是硬约束。

仿真初始速度限制使用比论文实验更保守的值：

```yaml
ur10e_adaptation:
  qdot_limit_initial_rad_s: 0.05
  qdot_limit_after_sim_validation_rad_s: 0.15
  force_ladder_N: [0.5, 1.0, 2.0, 5.0]
```

### E3. UR10e MuJoCo 轨迹

先复现实验 #1/#2，再做 #3/#4：

1. cycloid：论文实验 #1 的形状，缩放后适配 UR10e 工作空间。
2. figure-eight：论文实验 #2 的形状。
3. circle with material change：论文实验 #3 的圆形轨迹。
4. cardioid with material change：论文实验 #4 的心形轨迹。

每个轨迹都必须先通过：

- 无接触 dry-run。
- 接触静止 force ladder。
- 低速接触轨迹。
- 目标力 5 N 接触轨迹。

产物：

- 每个轨迹一个 run 目录。
- 保存 MuJoCo 状态、控制输入、接触力、末端位姿、关节角速度、失败原因。
- 生成 paper-style 图和 UR10e-specific 图。

## 8. Phase F: 测试和验收

单元测试：

```bash
pytest tests/test_finite_time.py
pytest tests/test_kinematics.py
pytest tests/test_contact_sign.py
pytest tests/test_orientation_error.py
pytest tests/test_constraints.py
```

集成测试：

```bash
python scripts/run_fig5_r_sweep.py --smoke
python scripts/run_ur10e_mujoco_adaptation.py --smoke
```

验收指标：

- Fig.5：r sweep 完整，收敛趋势正确。
- UR10e MJCF：headless load、joint/frame/TCP 检查通过。
- 接触模型：法向力符号、单位、frame 映射通过测试。
- UR10e MuJoCo：force ladder 至少通过 0.5 N、1 N、2 N、5 N；四类轨迹在 2 N 和 5 N 两个目标力下稳定运行。
- 没有隐藏 saturation：如果控制器输出被裁剪，报告中必须记录裁剪比例。
- 每个 run 的 config、git diff、环境依赖和随机种子可追溯。
- Fig.6 paper-style 六子图不作为近期验收项。

## 9. Phase G: 实机前只读验证

这部分不能让机器人运动，只做通讯、状态、传感器和坐标系确认。

检查项：

1. Dashboard 连接。
2. RTDE read-only 连接。
3. `actual_q`、`actual_qd`、`actual_TCP_pose`、`actual_TCP_force` 可记录。
4. OnRobot 连接路径确认。
5. 工具安装状态、线缆状态、急停和保护停止恢复路径确认。
6. TCP 和 payload 不在此阶段写入实机，除非另开 SOP 并手动确认。

参考已有只读成果：

- `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly`
- `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_builtin_force_maxfreq_60s`

实机前必须补齐：

- UR10e 当前 TCP 实测值。
- EOAT 实测质量、质心、接触点。
- OnRobot 是否用于反馈，还是只用于外部记录。
- 如果使用 OnRobot 高速数据，必须先写 no-bias/no-filter-change logger。

## 10. Phase H: 实机运动门槛

真实 UR10e 运动不属于默认执行范围，必须单独确认。进入前需要满足：

1. Fig.5 finite-time r sweep 已通过。
2. UR10e MuJoCo smoke、force ladder 和四类轨迹已通过。
3. UR10e read-only 日志证明状态稳定。
4. TCP/payload/接触头实测完成。
5. 低速、低力、可手动中止的 SOP 写完。
6. 用户明确接受风险并确认现场可操作。

实机运动顺序：

1. no-contact joint/Cartesian dry-run，速度不超过 `0.05 rad/s` 等效限制。
2. stationary contact，目标力 `0.5 N`。
3. stationary contact，目标力 `1 N`。
4. stationary contact，目标力 `2 N`。
5. stationary contact，目标力 `5 N`。
6. 短轨迹 5-10 s，目标力 `1 N`。
7. 短轨迹 5-10 s，目标力 `2 N`。
8. 完整轨迹，目标力 `5 N`。

任何阶段出现以下情况立即停止：

- protective stop
- force overshoot 超过计划阈值
- TCP/payload 明显不对
- 线缆拉扯
- 接触点滑出或碰撞非目标区域
- 控制器持续饱和

## 11. Phase I: UR10e 完整改编实验

最终实验矩阵：

| 实验 | 轨迹 | 目标力 | 表面 | 目标 |
| --- | --- | --- | --- | --- |
| E1 | cycloid | 5 N | 单一材料 | 对应论文实验 #1 |
| E2 | figure-eight | 5 N | 单一材料 | 对应论文实验 #2 |
| E3 | circle | 5 N | 多材料 | 对应论文实验 #3 |
| E4 | cardioid | 5 N | 多材料 | 对应论文实验 #4 |

> 备注：本矩阵是 §10「实机运动门槛」步骤 8（完整轨迹 5 N）完成后的稳态目标，不是绕过中间力梯度。任何 E1–E4 执行前都必须先走过 §10 的 0.5/1/2/5 N stationary 与 1/2 N 短轨迹梯度，并把每一档的 raw data + protective stop / saturation 状态归档到 `runs/`。E1–E4 与论文实验 #1–#4 的对应关系依赖 §4.1 的 PDF 核对，B2 闭合前本表的 #1–#4 映射只是草值。

每个实验至少记录：

- `actual_q`
- `actual_qd`
- `actual_TCP_pose`
- `actual_TCP_force`
- OnRobot force/torque，如可用
- 控制器目标 twist / qdot
- saturation flag
- contact state
- force error
- position error
- orientation error
- MIAE

对照基线：

1. constant impedance baseline
2. finite-time RNN controller

论文声称 MIAE 相比 constant impedance 降低 77.26%。UR10e 改编复现不能要求数值完全相同，但必须报告：

- UR10e MuJoCo 的 MIAE 对照。
- UR10e 实机的 MIAE 对照。
- 差异原因：平台自由度、力源、接触材料、TCP、速度限制、控制周期。
- paper-faithful MuJoCo 的 MIAE 对照后置；A2 恢复前不作为近期报告缺口。

## 12. 报告结构

最终报告放在：

```text
reports/tase_finite_time_ur10e_mujoco_reproduction_report.md
```

报告结构：

1. 摘要：说明当前是 UR10e-first adapted reproduction，A2/Fig.6 parity 后置。
2. 论文真值：列出所有从 PDF 抽取的参数。
3. MuJoCo 配置：UR10e 模型、接触、传感器、控制周期。
4. Fig.5 复现：曲线、收敛时间、与论文对比。
5. UR10e 改编：6 自由度改写、TCP/EOAT、控制器差异。
6. UR10e MuJoCo smoke、force ladder 和四类轨迹结果。
7. 后置项：Fig.6 paper-style parity 和 7 自由度模型未执行的原因。
8. 实机只读验证。
9. 实机接触实验，如果已执行。
10. 与 constant impedance baseline 的 MIAE 对比。
11. 不一致项和原因。
12. 复现实验包索引：所有 raw data、configs、figures、logs。

## 13. 立即执行清单

第一轮做 UR10e-first v1 仿真配置（按 §0.1，A1 默认 UR10e-first；paper-truth-extraction 是 v2 工作，不阻塞 v1）：

1. 创建 venv 并安装 MuJoCo。
2. 写出 `configs/paper_truth.yaml` 草值版，含 `pending_pdf_verify` 标记，不做 PDF 核对。
3. 实现 finite-time RNN 低维测试。
4. 跑通 Fig.5 r sweep（用草值，遇到 `pending_pdf_verify` 输出 warning 继续）。
5. 从 ROS 2 UR description 生成 UR10e **nominal** URDF（仿真不需要 calibration；calibration 在第三轮实机前才抓）。
6. 按 §A3 手工 MJCF + `<include>` mesh，跑 headless load。
7. 按 §A3.1 加入 OnRobot/EOAT primitive 几何与 TCP guess。
8. 实现接触平面和 force error，符号/单位/frame 单测通过。
9. 按 §C4 实装 `qpsolvers` + `osqp` QP，写 6 自由度 controller 与降级策略。
10. 跑 UR10e smoke 和无接触 dry-run。
11. 跑 force ladder 0.5/1/2/5 N。
12. 生成 v1 UR10e adapted 报告（明确标注非论文原平台复现，列出 v2 TODO）。

第二轮做 UR10e MuJoCo 轨迹扩展：

1. 跑 cycloid。
2. 跑 figure-eight。
3. 跑 circle with material change。
4. 跑 cardioid with material change。
5. 做 constant impedance baseline 对照。
6. 报告 MIAE、saturation、约束和失败模式。

第三轮才进入真实 UR10e：

1. read-only 状态确认。
2. TCP/payload/接触头实测。
3. no-motion force logging。
4. 低力接触 SOP。
5. 用户确认后再做低速接触实验。

## 14. 需要和你确认的问题

**待用户在本会话再确认的策略调整**（原计划从 session `019e5065` 沿用为「已确认口径」，但记忆 [[rnn-f2-code-not-final]] 的双分支策略是 paper-first 与 code-first **并行推进**，与下表的单线安排冲突；记忆 [[decision-points-need-full-context]] 要求策略级决策必须在本会话重新解释并确认）：

1. A2 paper-faithful 7 自由度 MuJoCo 模型后置，不作为近期第一步。
   - 原策略：paper-first 与 code-first 并行推进；
   - 新策略：UR10e-first 单线，paper-faithful 完全后置；
   - 影响：放弃近期 Fig.6 parity 与论文 7DOF MIAE 直接对照，节省 Franka 模型选型与 7DOF 算法移植；
   - 反向风险：若后续要恢复 paper-faithful，需要重新走 §A2 / §A2.1。
2. Fig.6 paper-style 六子图近期不做，不作为当前验收项（与上同源）。
3. 当前实现优先级是 UR10e MuJoCo（与上同源）。

仍需后续确认：

1. UR10e 实机反馈力源优先用哪个：UR 内置 `actual_TCP_force`，还是 OnRobot HEX？当前默认是 MuJoCo/算法阶段不绑定硬件力源；实机阶段先用 UR RTDE 500 Hz 做控制侧记录，OnRobot 作为外部验证。
2. 接触对象准备用什么材料/表面？如果要复现论文多材料实验，需要先定单一材料验证件和多材料边界。
3. EOAT v13 的 TCP 是否已经有实测数据？如果没有，85 mm 只能继续作为仿真 guess，不能用于真实运动。

## 15. 完成判据

不能只以“程序能跑”作为完成。

**v1 完成判据**（按 §0.1 范围）：

1. Fig.5 r sweep 有可重复数据、图、收敛时间表（草值参数下）。
2. UR10e MJCF 可加载，joint/frame/TCP 检查通过。
3. 论文参数草值和本地实现参数逐项可追溯（不要求 PDF 核对）。
4. 接触力符号、单位、frame 都有测试。
5. UR10e 6 自由度改编写明了与论文 7 自由度算法的差别。
6. UR10e force ladder 和四类轨迹通过近期目标力矩阵（v1 仅需单一材料解析平面）。
7. 实机前只读验证完成。
8. 实机运动只在单独 SOP 和用户确认后执行。
9. v1 报告包含成功项、失败项、不一致项和原因，**并附 §0.1 v2 TODO 列表**。
10. Fig.6 六子图和 paper-faithful 7 自由度 MuJoCo parity 明确列为后置项，未完成前不声明论文原平台严格复现。

**v2 完成判据**（在 v1 通过、用户决定推进精确化时启用）：

11. *paper-truth-extraction* 已完成并归档到 `configs/paper_truth.yaml`，每条值标注 PDF Section / 公式 / 表号；orientation_signal 维度歧义有书面 resolution；轨迹 #1–#4 映射经 PDF 核对（B1/B2/B3 全部闭合）。
12. §14 A1 已在彼时会话正式拍板（UR10e-first 单线 vs paper-first‖code-first 并行）。
13. EOAT TCP / 质量 / 质心实测；接触面多材料件设计完成；OnRobot/EOAT 几何由实测或官方 CAD 取代 primitive 近似。
