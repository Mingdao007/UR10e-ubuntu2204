# TASE 力控精度研究决策树（2026-09-26）

本树记录已观察到的分支与当前可执行下一步；它不是实机授权或完成结论。RPSFC/SFC/DSFC 静止漂移实验与 TASE 接触力误差分开处理。

```mermaid
flowchart TD
    A[先确认协议与通信证据] --> B{完整 60 s PATH、550 bins、460 Hz 门槛和 joint Home 都通过?}
    B -- 否 --> C[保留 attempt 失败性质；修通信或阶段交接，不调力控参数]
    B -- 是 --> D[R32 A: RNN、5 N、Md 9.5653、Bd 693.6559、积分 1.0]
    D --> T{现有完整圈是否带分层实时调用计时?}
    T -- 否 --> U[同一 A 配置做一次计时归因圈；先清空共享 CPU/I/O 时序 lane]
    T -- 是 --> E{固定输入重放能否隔离闭环力误差的因果来源?}
    U --> E
    E -- 否 --> F[R31/R32 已见控制输入高于评分滤波，但这不是因果证明]
    F --> G[保持 live A 与保护值；离线分开 envelope、task cap、最终 Jqdot]
    E -- 是 --> H[选择一个因素做候选；保护边界保持不变]
    G --> M{法向二阶 M/K/B 可辨识?}
    H --> M
    D --> I{QP 解时序合格且实际关节速度门通过?}
    I -- 解超时或不可行 --> J[保留失败；定位数值/约束冲突，不放宽边界]
    I -- 否，跟踪失败 --> K[R31 两个 QP 已完成路径但 qd correlation < 0.900]
    K --> L[不重复原 profile；先离线定位 J2/J4/J5 command-to-actual 差异]
    H --> M{法向二阶 M/K/B 可辨识?}
    M -- 否 --> N[暂停 zeta 派生参数；设计有界局部 normal excitation 或仅做已标明的 K/B 网格]
    M -- 是 --> O[比较 M/B/K 候选，保留配对和失败分母]
    L --> P[用同一 TASE 法向任务离线组合切向 SFC/DSFC]
    P --> Q{闭环/约束/时序和新数值都通过审阅?}
    Q -- 否 --> R[保持 offline_only；不接入 live registry]
    Q -- 是 --> S[单 writer、fresh package/Home/safety gate 后做配对 live]
    O --> P
```

## 当前证据账本

| 分支 | 已有 evidence 与身份 | 当前结论 | 缺口 / 下一步 |
|---|---|---|---|
| RNN A 基线 | [R32 A attempt](../runs/tase-r32-rate460-a-v3-capture-evidence-only-20260926T171700HKT/attempts/0001/)：`TASE_RNN_MATURE`，Figure-eight `r013_60_rate460`，60.0 s，550/550 bins，5 N，`Md=9.565272137974492`、`Bd=693.6559295653944`、integral `1.0 N·s`，TP consumed 480.936 Hz，joint Home verified。 | 正式 `[5,60)` MAE `1.098445 N`；同次 `[0,60)` feedback sample MAE `1.126476 N`。这是真实完整 A 单元。 | Kunwei feedback 不是独立 task-force truth。继续保留 sensor channel、force norm 和 normal force 的口径边界。 |
| RNN A force input 与运动实现 | [R31/R32 channel alignment](tase_r31_r32_force_channel_alignment_20260926.md) 与 [R32 force-to-motion chain](tase_r32_signal_to_motion_chain_20260926.md)：四个完整 A 圈中 `max(filtered, measured)` 比评分滤波高 `0.808–1.020 N`；R32 task normal cap 事件占 `22.60%`；final `Jqdot` 的 normal task-command residual p95 `2.802 mm/s`。 | 重复出现的信号差、速度限制和最终实现残差已被观测到。固定输入滤波反事实改变命令，但不产生闭环 MAE，不能分配它们对误差的因果份额。积分状态到 cap 的比例为 `7.03–10.32%`，单凭此不能判定 windup。 | 不改力输入 envelope、不提高速度上限、不改积分或求解器。先用新计时 trace 完成发布/接收/TP 分层归因，再离线逐个拆 envelope、task cap、final `Jqdot`。 |
| RNN 命令复算 | [R32 output/slew replay](tase_r32_rnn_output_slew_replay_20260926.md)：29,989 sealed ticks；host slew 限幅 11 tick。 | 旧 replay 的差异来自把 slew 错写成 `scale*qdot`；运行式是 `previous + scale*(qdot-previous)`，修正后 29,989/29,989 匹配。无需因这 11 tick 改控制器。 | 该结果只验证本地实现/packet mapping，不证明论文方程或 slew 对 MAE 的因果效果。 |
| QP inner solver | [R31 QP comparison](tase_r31_qp_motion_gate_comparison_20260925.md)：BO-10/B 和 matched-A 两圈均为 rate460、完整 60 s、550 bins；solver max `0.0926/0.1001 ms`，无 >1 ms solve。 | 正式 motion gate 是 consumed-sequence `corr(command, actual_qd) >= 0.900`。两圈仅 `0.840132/0.848607`，所以 MAE `0.914710/0.950139 N` 都是诊断数据；失败集中在关节跟踪差异，不能以低 MAE 宣称优胜。 | 不重跑同一 QP profile。先查逐关节 command→target_qd→actual_qd 及 final slew 对应，尤其 J2/J4/J5；硬 equality、joint/slew 保护和 1 ms deadline 保持不变。 |
| QP scheduler outlier | R31 报告记录 30,000 次离线 solve 最大 `0.04601 ms`；两次 live solve 最大均低于 `0.11 ms`。旧 106.1 ms 未重现；GC-enabled harness 曾自生 1.4–1.5 ms spikes。 | 证据不支持把两次 live QP 跟踪失败归因于 solver deadline；旧 106.1 ms 根因仍未知。 | 新调度试验必须与共享 CPU/I/O 负载隔离，不能把旧 outlier 或 host 周期误当作 controller solve 时间。 |
| 法向 M/B/K | [R32/R31 normal-axis identifiability](tase_normal_axis_model_identifiability_20260925.json)：自然 Figure-eight closed-loop force-vs-position 拟合中 `K-only R²` 近零，M/K/B 符号和量值随平滑窗、半程明显变动。 | 当前数据不能识别 5 N 工作点的有效法向 M、K、B；不支持从 `zeta=0.707` 推导 live 参数。 | 设计独立、有界的局部法向响应激励；具体位移/速度/持续时间/停线必须先绑定保护与审批。若不可辨识，只可做显式 K/B 网格，不称作二阶模型辨识。 |
| SFC/DSFC + TASE | [R32 sealed trace replay](tase_r32_live_trace_controller_replay_20260926.json) 用同一 RNN feedback trace 对 SFC/DSFC 做 open-loop counterfactual；每 tick 只做一次最终 joint realization，未使用真实曲面或 task-force 真值。当前 [composed adapter](../tools/tase_sfc_composed_adapter.py) 和注册角色仍标为 offline。 | 组合接缝可以离线复算；它不构成闭环力控结果或实机改善。默认 `120 N/m` path stiffness 是离线参数，不是 live-approved。 | 保持 TASE 法向/姿态优先；SFC/DSFC 只供切向任务，并经同一 final solver。补齐同 R32-A normal identity 的 replay、约束与误差敏感性；所有新 live stiffness/law 参数先列数值、单位、来源、作用和 replay。 |

## 当前研究选择

1. 已通过的 R32 A 与 no-motion resident start-check 保留为当前安全/性能基线；resident check 不算新的 force-control attempt。
2. QP 的旧 profile 已有两个完整但 motion-gate-failed 的实机结果，不再无变化复跑。保持门槛与 bounds，先完成 per-joint 实际速度链路诊断。
3. M/K/B 不可辨识分支不做 `zeta` 派生 live sweep。SFC+TASE 继续 offline-only，直到组合误差、实际步长积分、参数身份与 live gate 均闭合。
4. 在线学习仍等待 Google Drive 归档/容量证明；它不能和 live writer 并行，也不能跳过相同物理门槛。
5. R32 原始圈已通过聚合速率门槛，但没有此次新增的逐阶段调用计时。代码层 marker lifecycle focused suite 已 **101 passed**；修复仅保留已有 baseline 成功计数，提交 `29796f5f`。准备好的下一次 TASE 时序归因圈尚未启动。
6. [Power-on resume checkpoint](tase_poweron_resume_checkpoint_20260926.md) 记录机器人已上电、程序 STOPPED、joint Home、R31 TP triplet fresh read-back 和一张 OctoStream frame；它是只读检查点，不是新 attempt。Kunwei fresh baseline 和连续 observer 仍须在下一圈前建立。

最近 offline SFC+TASE 验证：`test_tase_sfc_composed_adapter_timing.py`、`test_tase_live_trace_sfc_dsfc_replay.py`、`test_contact_method_registry.py` 共 **22 passed**。验证覆盖观测间隔按不超过 4 ms 的零阶保持子步积分，超出 freshness bound 拒绝，以及注册身份仍属 offline；它不构成 live acceptance。

当前待做的计时归因圈受共享资源 lane 限制：2026-09-26 19:59 HKT 仍有一个无关 archive compaction 进程占用单核。保留其运行控制权，不与它并发做正式 timing acquisition；进程结束后重新 fresh-check 再启动。
