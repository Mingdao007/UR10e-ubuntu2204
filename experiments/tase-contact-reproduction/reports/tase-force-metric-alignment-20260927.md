# TASE 5 N / 60 s 指标对齐阶段报告 — 2026-09-27

## 结论

现有正式分数能从封存的 controller trace 复算；但它使用的是**由 Kunwei KWR75 反馈生成的低通滤波 normal force**。未滤波 KWR75 的逐样本和 100 ms-bin 误差明显更高。两套数值描述不同信号，不应混称为同一个 MAE，也都不是独立测力真值。

当前没有新策略达到“相对同条件 A 改善至少 0.10 N 且配对 95% CI 下界大于 0”。参数候选 B 未胜过 A；独立十对积分比较反而支持 1.0 N·s 的 A 优于 0.5 N·s。法向二阶模型仍不可辨识，暂不把阻尼比 0.707 换算成 live K/B。

## 协议与数据范围

固定身份为 `TASE_RNN_MATURE`、`r013_60_rate460`、5 N、60 s Figure-eight、正式窗口 `[5,60)`、Kunwei KWR75。官方分数是 550 个完整 100 ms bin 的 MAE。KWR75 为反馈来源，不是独立 task-force reference。

| 封存资料 | 物理尝试 | 完整 PATH | 正式合格 | 频率与封存 |
|---|---:|---:|---:|---|
| A–E 积分筛选 | 25 | 24 | 20 | 20 个合格单元均有 550/550 正式 bin；4 个因时序不合格，原失败保留 |
| 调参候选 A/B 五对确认 | 11 | 10 | 10 | 10 个合格单元均通过 460 Hz，550/550 正式 bin；1 次 observer stale 失败保留 |

两份独立 reducer 结果逐 attempt 校验 seal 字段，没有 seal 不一致。每个合格单元的 raw KWR75 都覆盖完整 `[0,60)` 600/600 个诊断 bin。未合格或未完成的尝试没有被填造正式分数。

## 为什么三种 MAE 不相等

R013 正式 550-bin 分数用每个周期的 `filtered_normal_n` 先分箱、再算 MAE。封存 trace 的 `normal_force_filtered_n` 能复算该分数；十对确认的最大绝对重建差为 **0.00033 N**。Raw 诊断则对 `-corrected_wrench_n_nm[2]` 计算：逐样本 MAE 对应 1 kHz KWR75 样本，bin MAE 对应每 100 ms 先取平均的 raw KWR75。它与正式分数的主要差别是信号是否滤波，不是 100 ms 分箱实现错误。

五对有效确认单元的组均值如下。A 是 incumbent 参数 `Md=9.5653, Bd=693.6559`；B 是 autotuner 参数 `Md=12.1693, Bd=769.7092`。两者积分策略都是 legacy 1.0 N·s。

| 指标 | A | B | 解释 |
|---|---:|---:|---|
| 正式 `[5,60)` filtered 550-bin MAE | 1.041 N | 1.175 N | 主指标；B 没有改善 |
| `[0,60)` filtered 600-bin MAE | 1.053 N | 1.184 N | 包含 PATH 前 5 秒 transient 的同滤波层诊断 |
| `[5,60)` raw KWR75 逐样本 MAE | 1.967 N | 2.218 N | 每个 1 kHz 样本分别计算绝对误差 |
| `[5,60)` raw KWR75 100 ms-bin MAE | 1.782 N | 2.033 N | 先求每 bin raw 均值，再计算绝对误差 |
| `[0,60)` raw KWR75 逐样本 MAE | 2.003 N | 2.258 N | 全 PATH 样本诊断 |
| `[0,60)` raw KWR75 600-bin MAE | 1.815 N | 2.072 N | 全 PATH raw 诊断，600/600 bins |

raw 逐样本 MAE 比 raw 100 ms-bin MAE 高约 **0.185 N**：先在 bin 内平均会抵消 bin 内正负波动，所以 `mean(abs(error))` 高于 `abs(mean(error))` 是预期现象。raw 100 ms-bin 与正式 filtered score 的差则约为 **0.74 N（A）和 0.86 N（B）**，来自滤波层不同。十次有效尝试两种窗口的 bin coverage 均为 100%。

配对正式分数差为 `A−B = −0.134 N`，95% CI `[-0.305, 0.037] N`。负值表示候选 B 的 MAE 更高；区间也不支持 B 胜过 A。该结论针对 sensor-feedback normal-force MAE，不宣称独立接触力真值。

## 已完成的积分比较

25 次 A–E 筛选中，筛选均值及诊断如下（仅对正式合格单元取组均值）：

| 组别 | 正式合格次数 | 正式 `[5,60)` filtered 550-bin | `[0,60)` filtered 600-bin | `[5,60)` raw KWR75 逐样本 MAE | `[0,60)` raw KWR75 600-bin | Coverage |
|---|---:|---:|---:|---:|---:|---|
| A — legacy 1.0 N·s | 4/5 | 0.977 N | 0.983 N | 1.889 N | 1.720 N | 合格单元 550/550、600/600 |
| B — legacy 0.5 N·s | 3/5 | 1.140 N | 1.135 N | 1.868 N | 1.691 N | 合格单元 550/550、600/600 |
| C — legacy 0.1 N·s | 4/5 | 1.756 N | 1.742 N | 2.287 N | 2.109 N | 合格单元 550/550、600/600 |
| D — conditional double clamp | 4/5 | 1.235 N | 1.253 N | 1.935 N | 1.794 N | 合格单元 550/550、600/600 |
| E — integral off | 5/5 | 1.929 N | 1.897 N | 2.478 N | 2.261 N | 合格单元 550/550、600/600 |

筛选规则先按完整数排序，因此 E 因 5/5 完成而入选；这不代表 E 精度最好，事实上它的正式 MAE 最差。该筛选不提供显著性结论。各组 `[5,60)` raw 100 ms-bin MAE 和 `[0,60)` raw sample MAE 也保存在 reducer JSON 中；原筛选账本为 [screening-results.json](../runs/tase-r31-integral-screening-20260925T1825HKT/screening-results.json)。

另一份独立的十对 A/B 积分确认比较的是 A=1.0 N·s 与 B=0.5 N·s，Md/Bd 相同。A 相对 B 的平均改善为 **0.180 N**，配对 95% CI `[0.029, 0.330] N`。这支持保留 1.0 N·s 作为当前基线，不支持把 0.5 N·s 直接替换为新默认。它与前述“调参参数 A/B 五对”是不同实验，不能混用 arm 名称或配对记录。详见 [十对确认 reconciliation](tase_r31_integral_ab_rate460_10pair_evidence_reconciliation_20260926.json)。

完整 A–E 诊断值和逐 attempt 分母见 [A–E reducer JSON](tase-r31-integral-screening-r013-diagnostic-20260927.json)；五对参数确认见 [confirmation reducer JSON](tase-resident-confirmation-r013-diagnostic-20260927.json)。原始 attempt 与 receipt 未修改。

## 积分与速度限制记录

五个成功的 legacy A 参数 trace 中，积分状态最高到 `1.0 N·s`；每圈约 **5.9%–10.9%** 的控制 tick 达到上限的 98%，均值约 8.1%。旧 trace 的 `integral_state_clamped` 和 `integral_saturated` 都为零，不能据此断言没有发生 clamp：旧实现把 clamp 后状态写进 `integral_raw_state_n_s`，legacy 诊断分支也把这些 flag 固定为 false。

我已修正 [Step5d outer loop](../tools/step5d_paper_outer_loop.py) 的**诊断输出**：今后 legacy clamp 会记录未限幅试算值并正确标记 state clamp。没有改变控制输出；旧记录无法追溯出实际超过上限的原始值。

`published_task_space_saturated` 在 A trace 平均约 **20.9%** ticks。按代码定义，它表示显式 task-space 速度约束生效时，请求的法向速度与限幅后法向速度不同；这不等于关节速度限制，也不表示机器人 20.9% 时间停住。最终 `Jqdot` realization saturation 与 host slew flag 都约 0.024% ticks。现有数据没有证明 task-space 限幅是力误差的唯一成因。

## 接触阶段时间与法向模型

五对确认中的 11 个已记录阶段，state20 搜索中位 **9.456 s**（9.334–11.502 s），state21 latch/ramp 中位 **8.862 s**（8.860–8.864 s），entry 中位 **1.010 s**。state20 按既有程序执行两段搜索，接触需达到 0.5 N 或 0.7 N force norm 并连续确认 80 ms，最大行程 15 mm；它不是固定的 10 s qualification。state21 的历史 replay 显示 **8 s** 升力 ramp 加 **0.5 s** 连续放行，实测 8.87 s 与此一致。一次 300 s preparation refresh 的周转较长：额外准备发生在 state20 前，不是 state21 的隐藏等待。

既有 Figure-eight 二阶拟合明确拒绝用于 live 参数选择：`K/B/M` 拟合对平滑窗口和轨迹半程敏感，系数会明显变化；轨迹跨越不同 XY 接触位置，且位置是力反馈控制下的内生响应。当前证据不识别稳定的法向有效质量、刚度、阻尼。因此不做基于 `ζ=0.707` 的 live 换算扫参；先做固定 5 N 接触点的有界局部法向响应识别。详见 [模型可辨识性诊断](tase_normal_axis_model_identifiability_20260925.json) 及 [state21 release replay](tase_r013_rate460_state21_release_replay_20260925.json)。

## TASE-QP 已有实机记录的准入诊断

既有两次 rate460 TASE-QP 实机 attempt 都实际完成 60 s PATH、550 个正式 bin、通过 timing/safety/contact/return，并验证回到 joint Home；但两次都因现行 motion gate 不合格，保留为失败 attempt。它们的单圈 MAE 只能作为诊断值，不能作为 BO 成功观测或与 RNN A 的配对改善证据。

| attempt | 参数 / 积分 | 正式 MAE（诊断） | TP 消费频率 | task-space 与时延 | qdot motion gate |
|---|---|---:|---:|---|---:|
| `tase-r31-qp-rate460-20260925T1600HKT` | Md=8.59266、Bd=772.14737、legacy 0.5 N·s | 0.915 N | 481.1 Hz | XY p95 0.074 mm、max 0.284 mm；lag 9.97 ms | 0.840，fail |
| `tase-r31-qp-matched-a-rate460-20260925T1615HKT` | A 参数 Md=9.56527、Bd=693.65593、legacy 1.0 N·s | 0.950 N | 480.05 Hz | XY p95 0.077 mm、max 0.251 mm；lag 16.84 ms | 0.849，fail |

motion gate 是同一被消费 packet 的 published qdot 与 RTDE `actual_qd` 六关节合并 Pearson，门槛 0.900。对封存序号重新连接后，两次重算分别为 0.84010 和 0.84863；单 packet 偏移最多只将后者提高到约 0.849，因此不是简单的 host/RTDE 错位。两次任务空间位置误差和 qdot lag 都在现有限值内，相关性失败主要集中在关节 2、4、5；关节 5 的命令标准差约 0.00021 rad/s，低于实测速度噪声尺度，但关节 2 的 command-to-actual 误差也明显高于 RNN A。

同一 frozen trace 的输出诊断发现：matched-A QP 有 513 次 host slew 限制、1,857 次最终 realization 限制和 2,916 次 task-space saturation；RNN A 两个对照 trace 的 host slew / realization 限制分别只有 7/7 与 3/3 次。固定输入重放准确复现 QP 及 host slew 输出，但只是 open-loop command replay。把原有 slew bound 直接并入硬等式 QP 的离线反事实在 packet 9676 即不可行；原 attempt 与 solver 状态保留为失败，没有修改 gate 或放宽边界。详情见 [QP motion-gate 分析](tase_r31_qp_motion_gate_analysis_20260925.json)、[指令变化分析](tase_r31_qdot_output_transition_20260925.json)、[事件分组](tase_r31_qdot_event_condition_20260925.json)、[固定输入重放与 embedded-slew 反事实](tase_r31_cartesian_velocity_diagnostic_20260925.json)。

当前 OSQP live profile 是硬等式 `J qdot = twist` 加 joint-velocity box bound，sealed trace 中 Jacobian 每个 PATH row 都是 full rank。这个配置下可行时 qdot 解唯一，单纯改二次目标权重不会提供连续性；加入 previous-qdot 惩罚、任务 slack 或分层软任务都会改变数学问题，并新增用户要求先逐项讨论的数值。故当前 QP 分支维持失败状态，不再按原 profile 重复 live 圈。上述 QP attempt 的 robot-frame recipe 不含 UR 内置 `actual_TCP_force`；force MAE 使用 KWR75 feedback。

## 下一步

保留 A（`Md=9.5653, Bd=693.6559, integral=1.0 N·s`）为同条件参照。TASE-QP 的调度/序号归因和 frozen replay 已完成；原 profile 的 live 尝试仍因 qdot motion gate 不合格，不能作候选结论。下一步应先在既有 traces 上完成控制输出变化与 force error 的对齐，并形成带法向/姿态优先及显式退让的 QP 数学候选；不改变已冻结 profile 的数值，不发新的 QP live attempt。随后可继续 SFC+TASE 的 offline same-input replay。每个新 live 候选继续同时输出正式 filtered 550-bin、filtered 600-bin、raw KWR75 逐样本指标及 coverage；KWR75 仍只代表 feedback source。当前没有新的 live motion；任何实机实验都要重新通过 fresh owner、安全、rate460、完整 60 s 和同一 joint Home recovery gate。
