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

进一步对五条 A 的 **149,541** 个正式 PATH tick 做重叠统计：积分状态达到 1.0 N·s 上限 98% 的共有 **12,119（8.10%）** tick；其中 **10,283（84.85%）** 同时有法向 task-space saturation 标志，**10,204** 同时仍有正力误差。上限时 normal-force feedback 中位数 **2.411 N**，力误差均值 **2.079 N**。与此相对，真正的 `Jqdot` realization 或 host slew 限制并集只有 **13/12,119（0.11%）**。这说明“把积分上限调大”不会改变大多数已被 task-space 限速的 tick，且可能留下更多积分状态；但它不能证明一个经过输出反馈的抗饱和律会更差或更好，D 组的正式结果仍按原筛选表报告。

审计也厘清了两个容易混淆的标志：旧 A 运行的 `integral_saturated` 在这些上限 tick 仍为零，所以应按数值积分状态与有效上限判断；`integral_explicit_velocity_limit_active` 实际映射到“某种显式 task-space 速度限制已启用”，五条 PATH 中始终为真，不能单独当成 normal axis 饱和。判断力向的 task-space saturation 应看 `published_task_space_saturated`。五次 trace 的逐 attempt 计数和 hash 在 [integral/task-saturation overlap JSON](tase_r013_A_integral_task_saturation_overlap_20260927.json)。

D 组实际 runtime 也已核对：四个合格 attempt 的有效积分上限始终是 **0.5 N·s**，因为 `authority_error=0.5 N` 与 `kf=1` 先将 1.0 N·s state cap 收紧到 0.5 N·s；state cap 本身没有被触发。D 的 output-freeze 共 **21,462/119,901（17.90%）** tick，其中 **20,936（97.55%）** 同时是法向 task-space saturation；freeze 时 **91.5%** 仍有正力误差，中位反馈力 **2.517 N**。相反，真实 final `Jqdot` realization 和 host slew 限制合计只有 **7** tick。也就是说，D 当前主要在“上游法向速度限幅”时冻结，而不是在关节最终实现饱和时冻结。D 的合格圈 MAE 均值 **1.235 N**，A 为 **0.977 N**，B legacy-0.5 为 **1.140 N**；筛选结果没有配对显著性结论，但当前 D profile 不值得直接提升。详见 [D runtime freeze semantics JSON](tase_r013_integral_conditional_freeze_semantics_20260927.json)。这些关联不能证明 freeze 是 MAE 变差的唯一原因；后续若试 anti-windup，应明确区分 task-space cap 与 final realization/slew saturation。

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

我把 hard-task feasibility audit 扩展为接受封存的 `rate400`、`rate460` 两种 60 s 身份，并对两次 **rate460** QP 失败按各自原始 `command_timeline` 重算。bo-10 attempt 有 **577/27,491（2.10%）** PATH rows、matched-A attempt 有 **406/27,453（1.48%）** rows，在保持法向与姿态精确时，无法同时满足实测 host-slew box；两组都是 joint-only box 可行、加入 host-slew box 后不可行。这个比例低于全程，但足以让单 tick 严格 QP 路径不能完整满足原 task。其余可行行所需的最小 tangent slack 中位数和 p95 都为 0，最大分别 **2.512 和 2.442 mm/s**；这只是逐行可行性下界，不能当作已选 live slack。完整结果分别见 [bo-10 feasibility JSON](tase_r31_qpfirst_hard_task_feasibility_20260927.json) 和 [matched-A feasibility JSON](tase_r31_qpmatched_hard_task_feasibility_20260927.json)。

因此，单独放松 tangent task 并不能处理这 577/406 行；保持法向优先时，线性规划给出的 orientation 最小 slack 中位数 / p95 / 最大值为 bo-10 `0.001016 / 0.002773 / 0.007429 rad/s`、matched-A `0.000592 / 0.002120 / 0.003417 rad/s`。这些值只是历史约束冲突的尺度，仍需把“姿态允许偏差多少”作为研究选择和复验目标逐项讨论，不能直接抄作 live 容差。captured host-slew box 又依赖原先 QP 输出，替换 solver 后会改变后续 box；所以这两份 replay 仍非新 QP 的闭环评估或 MAE 证据。

## TASE+SFC/DSFC sealed-trace replay

既有 live-trace replay 以 A 基线一整圈、29,989 个封存输入样本作为两种 native tangent law 的**开环同输入 command replay**。两者读取同一份 baseline-corrected Kunwei feedback，输入 digest 相同；候选命令未反馈到机器人或下一帧观察，因此不是闭环 force-score 重算，`primary_force_metric_recomputed=false`。法向也是该 TASE profile 固定的 base-Z reaction axis，不是曲面法向测量。原始记录仍显示 KWR75 是反馈源，不是独立 task-force truth。

| replay | 正常方向切向泄漏峰值 | Joint bound-hit ticks | bound violations | RMS `Jqdot` 对目标 twist 残差 | 实机/正式 MAE |
|---|---:|---:|---:|---:|---|
| `TASE_RNN_MATURE+SFC_YIELD_V1` | 0 m/s | 29,825 / 29,989（99.45%） | 0 | 0.06286（混合 twist 单位） | 未执行；未重算 |
| `TASE_RNN_MATURE+DSFC_YIELD_V1` | 0 m/s | 29,825 / 29,989（99.45%） | 0 | 0.06284（混合 twist 单位） | 未执行；未重算 |

该 replay 证明切向投影在所记录轴上没有数值泄漏，且每个 tick 只调用一次 final realization；但两候选几乎每个 tick 都碰到一个 joint velocity bound，最大残差约 0.121。Native profile 的路径 restoring term `120 N/m` 以及离线 final-QP 权重不是本轮 live 批准值。此迹线不能用于声称 SFC/DSFC 改善 5 N 力跟踪，也不能通过只看零法向泄漏就升级为 live 候选。接入 live 前须先让相同受保护的最终实现能在完整 sealed input 上保留法向/姿态任务，并显著减少持续 bound hits；之后才可计算逐样本及 550/600-bin 指标。详见 [sealed TASE+SFC/DSFC replay receipt](../runs/tase-r32-sfc-dsfc-live-trace-replay-final-20260926T173500HKT/summary.json)。

### Fixed-Home replay correction

上述旧 replay 的法向/姿态计算没有复用 R31 live 的姿态策略。它把 TCP Z 轴对齐到 base-Z reaction normal；live A 则用 `ko` 反馈到批准的 Figure-eight Home rotvec。由于当前 Home 的 TCP Z 相对严格 `-base-Z` 倾斜约 **0.04824 rad**，旧 replay 人为引入约 **0.121 rad/s** 角速度。逐帧比较 29,989 个 sealed 输入，旧输出角速度与 live `requested_outer_twist` 的 RMS 差为 **0.12089 rad/s**，因此上述高限幅数值不能用于判断 SFC/DSFC。

我已将 native SFC/DSFC 离线候选绑定到 [canonical Home](../config/figure8_home_v1.json) 姿态，并复用 live 的 SO(3) 误差公式与已冻结 `ko=5`。新 replay 对全部 29,989 行的 TASE 角速度与 live 源 command 的 RMS 差降至 **1.15e-11 rad/s**、最大差 **3.98e-11 rad/s**。SFC 和 DSFC 都是 **0/29,989** 次 joint-bound hit、0 次越界；最大关节速度分别为 0.02188 和 0.02185 rad/s，`Jqdot` 残差 RMS 分别为 0.00356 和 0.00357（混合 twist 单位）。修正后的 [fixed-Home replay receipt](../runs/tase-r32-sfc-dsfc-fixedhome-replay-20260927/summary.json) 已验证 source seal hash、相同输入 digest 和每 tick 一次 final realization。

该结果只证明 posture 与命令可行性 replay 对齐；它仍是开环 counterfactual，候选命令没有反馈进下一帧，且 `primary_force_metric_recomputed=false`。它没有产生 SFC/DSFC 候选力 MAE，也没有证明真实力改善；过去的模拟力 MAE仍是原 proxy 下的历史结果，不能替代新姿态身份的真实比较。

同一 sealed 输入下另行计时了 native law 本身的 `YieldLaw.step`（包含 DSFC 原生迭代，不包含 provider、RTDE、录制或 final RNN realization）：SFC 29,998 次调用 median **12.3 μs**、p99 **21.6 μs**、max **54.8 μs**；DSFC 29,998 次调用 median **12.9 μs**、p99 **22.7 μs**、max **61.6 μs**，都没有单次超过 1 ms。该结果只说明 law step 本身不是明显的 1 ms 单次计算瓶颈，不能代替完整 live loop 的 460 Hz timing gate。

### 切向反馈与实际运动方向核对

我又对五条合格 A trace 的同步 KWR75 与 RTDE 数据做了留一尝试验证。每条按正式 `[5,60)` 聚成 550 个 100 ms bin；`actual_TCP_speed` 的切向方向与同一批 RTDE `actual_TCP_pose` 位姿差分在所有 bin 都同向，平均 cosine 为 **0.9978–0.9984**。因此前面的反向现象不是由 RTDE 速度符号或简单的速度/位姿方向错配造成。

用同一 pose 将 baseline-corrected Kunwei 切向力转到 base 后，它与实际 TCP 切向速度方向相反的比例为 **92.0%–95.8%**，mean cosine 为 **−0.807 至 −0.760**，沿实际运动方向的投影均值约 **−0.50 至 −0.47 N**。每条 trace 上，`F_t = c + B v_t` 的分轴速度项也稳定为负：x 约 **−182 至 −171 N·s/m**，y 约 **−147 至 −134 N·s/m**；速度模型单条内 R² 为 **0.50–0.65**。加上由 100 ms RTDE 速度差分得到的加速度后，R² 只小幅增加（约 **0.001–0.007**）。

更严格地，每次留出一整圈、用另外四圈拟合 `force_xy = intercept + B·actual_TCP_velocity_xy`，在留出圈的力残差 RMSE 比仅用训练圈常量力均值低 **33.1%–37.2%**，平均 **35.0%**。这确认了可重复的**运动相关 KWR75 切向分量**，但没有分离出它究竟是表面摩擦/接触反力、残留零偏、EOAT/传感器坐标误差还是闭环耦合；速度与控制器命令来自同一闭环，也不能据此把回归斜率当成真实摩擦系数。

因此，现有原始力驱动的 SFC+TASE 候选会在无外部推扰 baseline 上持续生成与路径相反的切向补偿，保留为**离线可行、live 不准入**。不直接翻转符号、不删掉偏置，也不把该相关模型当成已验证的 friction compensation；在线扣除可能同时抹去真实外力。可复算的 5-trace 结果及 source timeline hash 在 [tangent-velocity relation audit JSON](tase_r32a_kunwei_tangent_velocity_relation_20260927.json)，重算代码与 focused tests 分别在 [audit tool](../tools/audit_tase_kunwei_tangent_velocity_relation.py) 和 [tests](../tests/test_audit_tase_kunwei_tangent_velocity_relation.py)。

## 下一步

保留 A（`Md=9.5653, Bd=693.6559, integral=1.0 N·s`）为同条件参照。TASE-QP 原 profile 仍因 qdot motion gate 不合格；native SFC/DSFC fixed-Home replay 已消除姿态错配和关节限幅，但新增实际速度对照显示原始 SFC 切向输入会抵消路径运动，故该版不得 live。下一候选前先确认 KWR75 切向力符号/轴向标定，并在不触碰已冻结保护的情况下分离可重复运动相关分量与外部扰动；若无法辨识，就保留此分支为未准入，不凭符号翻转试跑。任何新增 live 权重、slack、阈值或路径刚度先单独列明和讨论。每个 live candidate 继续报告正式 filtered 550-bin、filtered 600-bin、raw KWR75 逐样本指标及完整率；KWR75 仍只代表 feedback source。当前没有新的 live motion；任何实机实验都要重新通过 fresh owner、安全、rate460、完整 60 s 和同一 joint Home recovery gate。

## PATH 相位误差的重复性：ILC 是否值得离线推进

我用五条完整、rate460、同参数 A trace 的 550 个正式 filtered KWR75 bin 做留一尝试预测。每条重算出的正式 MAE 与 receipt 相差不超过 **0.00015 N**；相位误差曲线的两两 Pearson 相关均值为 **0.410**（范围 0.304–0.548）。只用另外四圈形成 phase-indexed error profile，在留出圈的误差 RMSE 相比一个常量偏差基线平均降低 **13.9%**，五折范围 **2.7%–23.8%**；每圈 mean signed error 仍约 **0.93–1.19 N**，每个时间 bin 跨圈误差标准差中位 **0.617 N**、p95 **1.183 N**。

这说明固定路径上有一部分误差形状可重复，值得把 phase-indexed ILC 当作离线候选研究；但可重复性有限，数据没有识别“修改哪个命令会让力改变多少”。目前没有构造或执行 ILC 命令，也没有力指标改善结论。审计及五条 source timeline hash 见 [phase-error repeatability JSON](tase_r013_phase_error_repeatability_20260927.json)，可复算代码与 focused tests 见 [audit tool](../tools/audit_tase_r013_phase_error_repeatability.py) 和 [tests](../tests/test_audit_tase_r013_phase_error_repeatability.py)。

## 原始 campaign 再执行核验

我从两个原始 campaign 目录重新运行 canonical reducer `tools/analyze_tase_resident_evidence.py`，并将输出与已封存的 reducer JSON 做逐字段比较。积分筛选 **25/25** 次尝试、调参确认 **11/11** 次尝试的完整 JSON 均完全相同；attempt 身份一致，所有字段的最大数值差为 **0**，两边 `seal_field_mismatches` 均为 0。简短机器可读凭证见 [metric-alignment reverification JSON](tase_r013_metric_alignment_reverification_20260927.json)。

| Reducer | 原始 campaign | 尝试 / 完整 PATH / 合格 | 已封存 reducer SHA-256 |
|---|---|---:|---|
| A–E 筛选 | `tase-r31-integral-screening-20260925T1825HKT` | 25 / 24 / 20 | `1d70eec299e67067bea9191d07e7b6ea0a8fe9dc7baa6a5f5a6f846cf5b9fbf0` |
| 参数确认 | `tase-resident-autotune-A-rate460-20260926T1413Z/confirmation-rate460-e-v1` | 11 / 10 / 10 | `4bfd7533d97634920c6879c6de7b7800c05e43cb607e7c3e62135f114910d634` |

这是一遍对原始封存记录的确定性重算，用来确认数据来源、reducer 输出与已封存 receipt 一致；它不是第二种独立算法。两类 A/B 结果仍严格分开：五对是 **Md/Bd 调参候选**比较，A−B 均值 −0.1339 N、95% CI `[−0.3050, 0.0372] N`；另十对是**积分上限 1.0 对 0.5 N·s**，A−B 均值 +0.1796 N、95% CI `[0.0289, 0.3303] N`。后者支持保留 1.0 N·s incumbent，不代表调参候选胜出。两项力指标均来自 KWR75 反馈，不能解释为独立测力真值。
