# 精细接触中的受力让步与恢复：实现与离线证据

本分支保留原 UR10e、Kunwei F/T、工具、工件和通信设施，锁定任务为
**恒力控制 + 未知曲面 + 姿态柔顺**。SFC 是 baseline，DSFC/MSFC 是 proposal；
SFC_RADIAL 仅作几何消融。原 LAC 等数据保留，RNN 和冗余调度不进入主线依赖。

**当前交付是离线研究实现和机制筛选，不是机器人 pilot 或正式论文比较。**
已有机器人 checkout 的 dirty、历史证据和未提交 runner 都已保留；
本分支没有启动 Dashboard、RTDE、bridge、Load/Play 或运动。

## 已实现的控制结构

`YieldController.step(observation, reference, dt)` 共用法向估计、滤波、外环、
约束 QP 和状态接口，通过现有 C++ native binding 替换 SFC/DSFC/MSFC。
离线 runner 连接闭环 UR10e 运动学/接触模型；接入真实机器人的唯一 writer
仍须通过 transport qualification，当前没有第二条命令通道。
ROS2 不是此离线包的新依赖。

令估计外法向为 n，P = I - n n^T，测得环境作用力为 f，目标力为 Fd，
路径为 xd，柔顺偏移为 z。三种方法共同使用输入：

```
u = filtered(f) - Fd*n - Kp*P*(x-xd) - Kz*P*z
correction = selected_native_law(u, dt)
v = P*xd_dot + limited_normal_and_tangent(correction)
```

当前公共积分补偿增益和 Kz 均为零；积分补偿非零时有共同限幅。Kp=120 N/m。
Kz 的历史值 40 N/m 保留作回放与消融，原因见下方共同外环修正。
正常进给速度在非线性
动力学之后加入，不进入非线性阻尼。z 积分 QP 之后的指令修正，
它是控制器状态而不是测得让步位移；后者由真实模型 TCP 与配对 nominal 的差计算。
所有控制律状态保存在固定 base frame，姿态变化不旋转或重置这些状态。

### 实际方程与 native 映射

这里 w 是 native 内部速度状态，输出为 g*w，r = ||w||。

| 方法 | 连续形式 | 实际离散实现 |
|---|---|---|
| SFC | m*w_dot_i = u_i - mu*abs(w_i)^(n-1)*w_i | 现有 `sfc_step`，逐轴显式更新 |
| SFC_RADIAL | m*w_dot = u - mu*r^(n-1)*w | 同参数径向显式更新；一维退化与 native SFC 一致 |
| DSFC | m*w_dot = u - (a*r^(p-1)+mu*r^(n-1))*w | `ysfc_step` 的隐式径向 resolvent；不是另一份近似 Python 方程 |
| MSFC | m*w_dot = u - a*r^(p-1)*w - mu*(w^T A w)^((n-1)/2)*A*w | `smysfc_step` 的隐式机械求解与完整 memory |

MSFC 使用压缩力特征 psi(u)、历史 h 和对称结构 S：

```
h_next = (h + dt/tau_f * psi(u)) / (1 + dt/tau_f)
S_next = (S - dt*kappa*h_next*h_next^T) / (1 + dt/tau_r)
A = lambda_min*I + (1-lambda_min)*exp(S_next)
```

native 对结构负半定性、metric 和求解残差做检查；快照保存完整 22-slot token，
并绑定参数、native build、QP binary、公共设置和坐标定义。snapshot/restore
还包含滤波、法向估计、积分、柔顺偏移、姿态 roll anchor、QP warm state 和时钟。
非法快照、QP 异常或过期观测会回滚整个 tick，干预及释放不触发重置。

姿态以有限角速度跟随估计法向，并软保持初始 roll anchor。
此定义与本地 TASE 原文 Objective 2 / Eq. (14) 的法向对齐任务相符，
不等同于任意外力矩的旋转导纳。模拟器的点接触不产生接触力矩，
不能据此宣称工具面接触力矩控制或整臂碰撞柔顺。

### 共同模块及其限制

- 法向估计仅使用测得速度、接触力和初始 approach；曲面真值只在 evaluator 中。
  有接触/激励门控、切向约束更新，以及明确标注的有界力方向偏置修正。
  摩擦和外界干预仍会污染估计，不能将合力方向当作无偏真值。
- QP 共用 6D 等式和关节速度约束；仅真实 primal infeasible 时逐级降低切向和姿态进度，
  保留法向分量。deadline、非有限值和其他求解失败不会伪装成可降级的不可行。
  保留法向速度分量不构成接触力上限保证。
- 路径时钟持续前进，禁止恢复 preparation 来冻结时钟。记录实际 TCP 进度、
  路径误差、限幅和 QP 干预，不能靠指令完成率宣称任务完成。
- freshness 保留 20/80 ms 语义，同时独立检查 1 mm 几何延迟界。
  host decode receive clock、计算耗时和实际机器人执行不是同一项证据。

## 实验与可复算性

5 N、80×20 mm 八字路径、62.831853 s 周期；完整周期另有 1 s entry。
测试两组接触参数：8000 N/m + mu=0.15，以及 2000 N/m + mu=0.40。
这些是模型假设，不是工件辨识结果或允许损伤载荷。

先保留原参数结果，再使用每方法相同四个 gain factors `{0.03,0.1,0.3,1}`
作 6 s nominal screen。proposal 的低速项变体 `p=0.5,a=0.05` 在该 screen 前固定；
原 DSFC `p=0.1,a=1.2`、MSFC `p=0.07,a=1.2` seed 没有改写。
所有候选与所选完整参数见 `screen.json`。
这不是 24-unit Bayesian optimization，也不是独立 holdout。正式的
8 initial + 12 BO + 4 repeat 预算和至少 5 次重复仍保留在协议中，尚未消耗。

机制矩阵为 4 methods × 2 materials × 4 scenarios：正常、持续法向干预与释放、
持续切向干预与释放、短时斜向扰动。软件注入力与作用于模型动力学/传感器的外力
明确区分；两者都不是真实人类施力证据。

模型采用既有校准 UR10e FK/Jacobian、URDF inertia、假设的 PI 速度 servo、
单边弹簧阻尼接触、平滑 Coulomb 摩擦与传感器延迟。
初版 P-only servo 的稳态负载漂移影响全部方法，原结果及源码已留档，
没有作为 proposal 的负结果或正结果。PI servo 也没有经过实机辨识。

旧共同外环 Kz=40 的完整结果表、配对恢复指标、数值敏感度和 raw receipt 路径
见 [results.md](results.md)。其 32 周期矩阵、12 周期细化和 6 周期共同外环消融
使用的可追溯源码为 commit `72433730`。完整周期细化另见
[full-refinement.md](full-refinement.md)，不能以短程细化代替。
每份 raw receipt 带输入、控制状态、模型状态、native/源文件 hashes，
`manifest.json` 给出压缩原始文件 SHA-256。不要仅凭图形推断 controller 优劣。

## 运行方法

从本实验目录执行，命令均为离线操作：

```bash
scripts/contact-yield.sh provision
scripts/contact-yield.sh run --method MSFC --parameters config/contact_yield_candidates/msfc.json --scenario sustained_release_oblique --duration-s 0.6 --output runs/new-trial.json.gz
scripts/contact-yield.sh replay --input runs/new-trial.json.gz --output runs/new-replay.json
scripts/contact-yield.sh refine --input runs/new-trial.json.gz --output runs/new-refinement.json
```

当前 CLI 默认 8 个 plant substeps：控制/命令周期仍为 2 ms，接触模型积分为
0.25 ms。`--plant-substeps` 可显式改变该项，身份和回放会检查它。
此默认值本身不代表数值验收通过。
`config/contact_yield_candidates/` 保存本次冻结的三个离线候选；没有传
`--parameters` 时仍用历史 seed，不能把它当成本次所选候选。候选均未获硬件资格。

完整 seed screen/matrix 使用 `tools/contact_yield_mechanisms.py --output NEW_DIRECTORY`；
已有相同代码/参数的矩阵可用 `tools/resume_yield_mechanisms.py --root DIRECTORY --workers 4`
接续，已有 artifact 不覆盖。并行运行的 wall time 不是 realtime qualification。
`tools/check_yield_plant_refinement.py` 在固定 2 ms 控制周期下独立细分 plant，
避免将控制律步长效应与接触积分误差混为一谈。

复算上面历史结果时须使用 `72433730` 的代码和记录中的完整 settings，
不能把当前 Kz=0 默认当成历史 Kz=40。旧 v1 协议的 diagnostic entry 字段曾写为
0 s，但实际 runner 和每份 receipt 的 `preparation_protocol` 均为 1 s；
当前 v2 已修正，并把默认 diagnostic PATH 扩至 0.6 s，以保留释放后至少 0.1 s。
历史 screen 描述字符串把 MSFC seed p 简写为 0.1；实际加载的配置及 native
身份使用 0.07，当前描述已纠正。没有回写原始 receipts。

## 共同外环修正及观察到的代价

累计指令修正并不等于测得 TCP 偏移，外力引起的机器人位移及受限的路径进度会
使二者分离。在 Kz=40 的机制矩阵中，所有主方法持续切向干预后均未回到恢复带。
因此对三种方法共同去掉该附加弹簧，保留测得路径误差的 Kp 恢复项。
这是共同模块修正，不计作 proposal 的创新收益。

同一 stiff_low_mu 模型、8 个 plant substeps、冻结控制律参数下：

| 方法 | nominal 路径 RMS mm | 持续切向让步峰值 mm | 释放后恢复 s | 干预 trial 接触力峰值 N |
|---|---:|---:|---:|---:|
| SFC | 1.470 | 6.240 | 10.926 | 5.890 |
| DSFC | 1.340 | 5.831 | 2.660 | 5.947 |
| MSFC | 1.030 | 18.185 | 10.854 | 5.988 |

对应完整数据见 `offset-ablation-summary.json`。DSFC 在这里恢复更快，但接触峰值
略高；MSFC 允许更大让步，也产生更大任务偏离。此表仅是一个确定性工况的设计
证据，没有正式调参、重复试验和置信区间。相同精度/载荷水平的正式比较仍未完成。

旧 Kz=40 下，SFC/DSFC 全周期 4→8 substeps 的最大力差约 0.027/0.043 N；
MSFC 持续法向干预达到 3.516 N。公共外环已改变，因此旧数值不能直接当作
当前 Kz=0 的验收结果；新的共同外环数值检查见 [common-fix.md](common-fix.md)。
修正后 SFC/DSFC 在 nominal 和持续切向测试的最大力差分别不超过
0.036/0.010 N；MSFC nominal/切向约 0.091 N，持续法向仍达 **3.603 N**。
因此全任务的 pilot 推进条件仍未通过，不能以切向恢复的改善覆盖法向问题。
本次共保留 58 个 PI 模型完整周期运行，全部原始文件 manifest 已核对。

## 验证

当前 contact 回归 **144 passed**，只有现有 xacro deprecation warning。
包含未知法向输入隔离、实际步长、独立几何延迟、姿态随动、不重置 memory、
完整状态回放、非法状态回滚、QP 故障分类和径向 SFC 消融。
另由独立进程对 MSFC 的完整周期细分模型进行 **31,916 tick** 全状态回放，
无不一致；见 `full-state-replay.json`。这是相同实现的确定性回放，不是独立物理验证。

测试需禁用宿主 ROS pytest 自动插件，并显式提供既有 ROS Python 路径：

```bash
env -u PYTHONHOME -u VIRTUAL_ENV PYTHONPATH=/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv-contact-six/bin/python -m pytest -q tests/test_contact*.py
```

## 尚未达到的推进条件

完整闭环执行、快照回放和测试通过只说明工程链可运行。
必须另外建立正常任务精度/进度相当、干预收益可重复、数值变化不改变结论的证据，
才能进入 pilot。恢复实机前仍需 fresh hardware 状态、正确身份绑定的唯一 writer、
完整 transport qualification、短程接触及未知曲面姿态随动。
之后才依序 SFC → DSFC → MSFC，每次停止并确认后切换。

正式 24 paired units、holdout、5 次重复和论文贡献判断未完成。
具体推进状态见 `advancement.json`。下一项是分离 MSFC 法向闭环的 gain、
force-filter delay、限幅、接触积分与 memory 效应，随后重新检验完整任务收益；
当前数据不能把原因直接归给 memory，也不能通过放宽门槛推进实机。
收敛理论解释机制；软件接通、去 RNN 和本次平台移植不作为创新结论。
没有损伤或人体接触证据时，最终结论也只限于测得的性能改善。
