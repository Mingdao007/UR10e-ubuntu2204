# Direct Torque v4 真机经验汇总

本文是 TacDiffusion native Remote Direct Torque v4 的单一经验入口。动态
stage 状态仍以 `STEP5D_TACDIFFUSION_FLOW.md` 和
`config/step5_stage_table.json` 为准；本文件集中记录已经由代码、A/B
probe 或真机数据支持的可复用经验。

## 当前结论

- 2026-07-28 的 latest live no-contact chain 在当时的 runtime/safety gates
  下完成，但后续 offline audit 发现 applied-action RTDE registers 存在
  torn snapshot；因此该 chain 只保留为 historical transport/motion
  evidence，不能证明 coherent 12D action labels。
- 原先 `actual max norm / desired max norm = 17--21%` 只能作为 noise
  envelope，不能再叫 tracking response。排除 entry 20 ms 后的 joint
  fit 得到 `alpha = -0.79%`（2 s）、`0.63%`（7 s）和 `0.41%`
  （10 s），`R² <= 0.0021`，与 time-reversed null 同量级。
- 独立 calibrated Pinocchio cross-check 复算 10 s translation-only
  `J^T w`，maximum torque norm `0.239 Nm`，与记录 command
  `0.238 Nm` 接近；gross K sign、base-frame direction 或 Jacobian
  transpose error 不再是 leading hypothesis。
- 当前 leading physical hypothesis 是 `<=0.42 N` TCP command 落在
  near-zero breakaway friction/deadband/noise 区域；这仍需后续
  no-contact single-variable amplitude A/B 才能证明。
- 当前 source 已加入 action publication seqlock、50 ms torque-thread
  main-loop watchdog、150 ms entry-filter warm-up，以及 entry 前 100 ms 的
  `0.5 mrad` joint / `0.3 mm` TCP position-excursion fail-closed guard
  （fault `14`）。Autotune 受控停机并释放 writer 后，这个 source 已完成
  fresh compile/handshake/hold/ramp/reference no-contact live validation。
- 新增 RTDE motor diagnostics（`target_current`、`actual_current`、
  `actual_current_as_torque`、`joint_control_output`、`joint_mode`）没有把
  当前约 `0.2 Nm` command 从 motor noise 中辨识出来；这不等价于证明
  `direct_torque()` 没有执行。
- trajectory fidelity、contact control 和 expert-data eligibility 仍未
  通过；所有现有 capture 均为 `training_dataset=false`。

最新主要证据：

- `runs/tacdiffusion/direct_torque_v4_motor_diag_compile_probe_20260728/evidence.json`
- `runs/tacdiffusion/direct_torque_v4_motor_diag_normal_baseline_20260728/evidence.json`
- `runs/tacdiffusion/direct_torque_v4_motor_diag_hold_100ms_20260728/evidence.json`
- `runs/tacdiffusion/direct_torque_v4_motor_diag_ramp_0_2mm_500ms_20260728/evidence.json`
- `runs/tacdiffusion/direct_torque_v4_motor_diag_reference_2s_20260728/evidence.json`
- `runs/tacdiffusion/direct_torque_v4_motor_diag_reference_2s_20260728/tracking_and_actuator_audit_v1.json`

## 1. 先区分四种频率

不得再用单一“500 Hz”概括整个系统：

- RTDE output logging：真机约 `500 Hz`；
- dedicated torque thread：真机约 `481--500 Hz`；
- main controller-law refresh：真机约 `198--200 Hz`；
- Kunwei native frame stream：真机约 `1002--1007 Hz`。

500 Hz RTDE rows 不等于 500 Hz controller-law update，1 kHz Kunwei frame
也不等于具有 1 ms causal robot/force alignment。

## 2. Direct Torque 必须由极简独立 thread 连续调用

已验证的结构是：

- `torqueThread()` 只读取共享 torque 并调用 `direct_torque()`；
- torque thread 内不放 packet parsing、Jacobian/controller computation 或
  `sync()`；
- main thread 更新共享 command、检查 guard、写 evidence registers；
- 退出 torque 后才执行 position handoff。

把所有计算与 `direct_torque()` 放在同一个约 200 Hz main loop 会制造
torque-call 空窗，不符合当前已通过的 cadence contract。

## 3. 启动姿态不能直接插值 rotvec 分量

等价的 `+pi` 与 `-pi` axis-angle 表示可以在分量上相差约 `2*pi`。
直接线性插值会制造不存在的大角度旋转和明显响动。

当前 no-contact entry 没有 orientation trajectory，因此采用：

- translation-only blend；
- orientation 固定为 fresh measured entry orientation。

未来确实需要 orientation trajectory 时，应使用 quaternion/SO(3)
shortest-path interpolation，并增加 antipodal 与 `pi` branch regression。

## 4. 加速度 guard 是防线，不是根因修复

限制每 2 ms 的 `delta_tau` 或在超过阈值后停止，只能降低后果。已经
确认的根因修复包括：

- 连续 500 Hz torque-call thread；
- fresh actual-pose entry；
- translation-only entry blend；
- elapsed-time controller evolution；
- 正确的姿态表示；
- 没有 stale/zero-torque startup window。

derived joint-acceleration guard 仍保留为 defense in depth，当前上限为
`5 rad/s^2`。

## 5. zero custom torque 不等于控制器内部所有 torque 都为零

`direct_torque()` 仍由 UR 内部提供 gravity compensation。no-contact entry
把 UR viscous/Coulomb scales 设为零，目的是隔离 friction/stiction
injection，而不是关闭 gravity compensation。

因此：

- 不把 position-control 的 `target_moment` 直接复制成 Direct Torque
  command；
- 不把“第一帧 custom torque 为零”当作整机 torque 为零；
- payload/CoG 设置属于独立的机器人模型输入，必须按真机姿态数据验证。

## 6. Secondary Client 启动需要 fresh Primary start barrier

症状：

- source 已发送到 30002；
- RTDE 只看到上一 episode 的 `COMPLETE` 或 `FAULT`；
- runtime 保持 STOPPED；
- 新 lease/episode 从未 echo；
- `sent_sequences=0`，因此没有 Direct Torque 和运动。

2026-07-28 idle-only A/B：

- 仅增加 150 ms delay：exact source 不启动；
- 长时间提前打开 Primary observer：仍可能不启动；
- 在 Secondary send 前立即新建 30001 read-only observer，并保持
  150 ms：出现 `PROGRAM_XXX_STARTED` 和新 WAITING identity。

当前修复：

- 每次 30002 send 都与一个 fresh 30001 receive-only connection 绑定；
- Primary observer 从不发送任何字节；
- evidence 记录 connection、hold time 与 received bytes。

## 7. resend 必须满足严格的否定证据

最多允许一次 resend，且必须同时满足：

- fresh RTDE 持续报告 `runtime_state=STOPPED`；
- receiver state 仍是旧 `COMPLETE`/`FAULT`；
- 没有新 WAITING；
- 没有新 lease echo；
- 没有新 episode echo。

只要出现 PLAYING、WAITING 或任一新 identity 证据，就禁止 resend，防止
两个 receiver 重叠。

## 8. cadence 只能在 receiver 拥有的 active states 上统计

STARTUP 前的 output registers 可能来自先前加载或先前运行的程序。
历史 `524.0 s` 假 gap 就来自把 stale WAITING register 纳入全局最大值。

正确 cadence scope 是：

- `STARTUP`
- `TORQUE`

immutable 原始 evidence 不回写；修正只能生成新的 audit 或重新运行。

## 9. Kunwei 是当前实验 F/T source，但不是精确 1 ms 对齐

当前 live canary：

- 只使用 Kunwei software-baselined sensor-to-TCP SI wrench；
- 不使用 UR internal F/T 作为实验数据或 guard；
- 不执行 Kunwei tare/zero/filter/config write；
- 同时保存 raw frames 与 parsed CSV；
- 要求 parse error 和 dropped sync bytes 为零。

TCP 一次 receive batch 可包含多帧，同 batch 共用 host arrival timestamp。
因此 native rate 接近 1 kHz，但 `causal_1khz_alignment_valid=false`。

## 10. 安全/cadence PASS 不等于 tracking PASS

2026-07-28 原始 chain 数据：

- 0.5 s ramp：captured command 最大 `0.183 mm`，实际最大 `0.092 mm`；
- 2 s reference：captured command 最大 `0.634 mm`，实际最大
  `0.119 mm`；
- 2 s reference 最大 commanded joint torque 仅约 `0.205 Nm`。

因此当前问题不是“信号明显过大”，而是实际 Cartesian response 明显小于
command。新的单变量 isolation 保持空间轨迹、K、guards 和 torque
structure 不变，只把同一路径从 2 s 拉长到 10 s。实测：

- fresh 2 s：command `0.631 mm`，actual `0.099 mm`，response ratio
  `15.7%`，maximum derived joint acceleration `2.061 rad/s²`；
- 10 s：command `0.636 mm`，actual `0.132 mm`，response ratio `20.8%`，
  maximum derived joint acceleration `0.917 rad/s²`；
- 两者 maximum commanded joint torque 都约 `0.193 Nm`；
- 10 s endpoint actual displacement 仅 `0.029 mm`。

放慢 5 倍把最大推导加速度降低约 `55.5%`，但 response ratio 只增加
`5.0` percentage points，endpoint 几乎没有改善。因此短时轨迹加速度
不是 tracking 不足的主因。下一步应先离线检查：

- `K * pose_error` 的有效 wrench 是否符合设计量级；
- `J^T * wrench` 的 frame、单位、符号与实际 Jacobian 是否一致；
- UR `direct_torque()` command 的语义与当前 gravity/model compensation
  假设；
- damping、stiction 与 command echo 到 applied torque 的有效增益。

在这些量被解释前，不提高 torque、速度或 contact force。10 s run 仍是
`diagnostic_only`，不是新的 canonical acceptance 或 expert dataset。

对应 numeric sanity：
`config/direct_torque_v4_reference_10s_diagnostic_sanity.json`。

## 11. zero-friction 是 isolation profile，不是最终 tracking profile

UR 的 Direct Torque v2 文档明确给出：

- viscous 默认值：`[0.9, 0.9, 0.8, 0.9, 0.9, 0.9]`；
- Coulomb 默认值：`[0.8, 0.8, 0.7, 0.8, 0.8, 0.8]`；
- scale `0` 表示不做对应 friction compensation；
- custom torque 不包含 gravity，gravity 仍由机器人内部补偿。

官方来源：

- `https://www.universal-robots.com/articles/ur/release-notes/release-note-software-version-525x/`
- `https://www.universal-robots.com/manuals/EN/PDF/SW5_25_1/scriptmanualG5/script_directory_Poly5.pdf`

当前 `zero_isolation` 把两组 scale 都固定为零，是为了隔离早期 mode
transition、thread cadence 与 rotvec branch-cut 故障。它通过了安全/cadence
chain，但不能据此认为零 friction compensation 是最终控制配置。

现有 tracking 数据与官方语义一致地支持下一 A/B：

- `K=600 N/m`、约 `0.64 mm` error 只产生约 `0.38 N` Cartesian
  restoring force；
- 实测 maximum commanded joint torque 约 `0.193 Nm`；
- 2 s 拉长到 10 s 后 response ratio 仍仅从 `15.7%` 到 `20.8%`；
- 完全关闭 Coulomb/stiction compensation 是 weak response 的强候选。

因此新增独立 `ur_default_v2_diagnostic`，只恢复官方 V2 friction scales。
它必须从 100 ms hold 重新开始完整 ordered receipt chain；不得复用
`zero_isolation` 的 prior-stage receipt，也不得把诊断结果升级为 contact
或 expert-data acceptance。

实测更新：

- official-friction 2 s response ratio 为 `26.7%`，高于 zero-isolation
  2 s 的 `15.7%`；
- official-friction 7 s response ratio 又降至 `20.3%`，command
  `0.636 mm`、actual maximum `0.129 mm`、actual endpoint `0.059 mm`；
- 7 s 最大 commanded joint torque 仍仅约 `0.194 Nm`；
- 7 s 最大推导关节加速度 `1.686 rad/s²`，Kunwei 最大零基线力范数
  `0.619 N`，没有 guard 或 cadence failure。

因此恢复官方 friction compensation 对短 2 s response 有帮助，但没有
解决更长时间 tracking。此前多个 recent official-friction canary 有可听
声音，3 s 又被认为不足；但操作者明确报告 7 s diagnostic 没有听到异常
声音。因此“official-friction run 必然有声音”已被这次实测否定。7 s 相比
此前运行同时包含更长 time stretch 和 fresh ordered chain，不能仅凭一次
无声结果把改善归因给 acceleration、duration、entry/exit 或 friction
model；后续若继续定位，应保持单变量 A/B。

> 2026-07-28 offline correction：以上 `15.7%`、`20.8%`、`26.7%`、
> `20.3%` 均为 historical max-norm envelope，不再作为 tracking
> response 或 friction-effect evidence。相同配置的 directional/joint
> regression 结果见下节。

## 12. Max-norm envelope 不能代替 tracking coefficient

旧指标：

`max(||actual_xyz - actual_xyz_entry||) /
 max(||desired_xyz - desired_xyz_entry||)`

会把任意方向的 RTDE pose jitter 都计入 numerator，而且 sample 越多，
maximum 越大。当前每个 run 自己的 pre-torque WAITING noise RMS 约
`30.5--32.2 um`；active maximum displacement `92--136 um` 与 standstill
noise envelope 同量级。

新的 offline analyzer
`tools/analyze_direct_torque_v4_tracking.py` 同时报告：

- 旧 max-norm envelope，仅作 historical comparison；
- 固定 command direction 的 signed projection、orthogonal RMS 和
  correlation；
- 排除前 20 ms 后的 joint fit
  `actual_xyz = alpha * desired_xyz + intercept_xyz`；
- `R²`、optimistic IID standard error 和 time-reversed desired null；
- 每个 run 自身 WAITING noise floor；
- 可选 calibrated Pinocchio `J^T w` cross-check。

当前结果：

| Run | Legacy envelope | alpha | R² | reversed-null alpha |
|---|---:|---:|---:|---:|
| official-friction 2 s / entry-lowpass | 16.84% | -0.792% | 0.00201 | 0.343% |
| official-friction 7 s / no abnormal sound | 20.29% | 0.629% | 0.00144 | -0.502% |
| official-friction 10 s / entry-lowpass | 21.41% | 0.414% | 0.00059 | -0.437% |

因此当前数据不支持一个可重复的 Cartesian tracking response。它也不证明
绝对零响应；结论是 measured response 与 null/noise 尚不可区分。

## 13. Applied-action echo 必须使用 publication seqlock

Opus 5 high audit 发现并由 retained CSV 直接复现：

- active block 先写 state/counters，再写 float registers `26--43`；
- RTDE 500 Hz 可以在 block 中间取样；
- 10 s entry-lowpass 的第一 active row 已经是
  `state=STARTUP, control_update_count=1, torque_thread_tick_count=0`，
  但 `commanded_joint_torque_nm_0=0.1902146167` 仍来自前一 2 s run；
- 因此历史 `first custom torque` 与 `zero_custom_torque_at_entry`
  classification 无效，未来 expert action label 也必须先解决 coherence。

当前 offline repair：

- output integer register `29` 在 active publication 开始前写 generation；
- output integer register `33` 在 action/cadence registers 全部写完后写同一
  generation；
- active row 只有 `begin == end > 0` 才是 coherent；
- host 保存所有 RTDE rows，但 action-derived metrics 只消费 coherent rows，
  并单独报告 rejected count/fraction；
- `entry_transition/v3` 只在 entry window 全部 coherent 时给 action
  PASS/FAIL，
  否则给 `INDETERMINATE`。
- joint/TCP position excursion 使用相同 20 ms window 的全部 physical
  RTDE rows 独立计算，不因 action publication incoherent 而丢失物理
  fail-closed 能力。

这修复的是数据一致性，不改变 torque law。

## 14. Torque thread 需要 main-loop staleness watchdog

独立 `torqueThread()` 解决了 torque-call 空窗，但旧实现若 main thread
卡住，会无限重复最后一帧 torque。当前 offline source 让 torque thread
监视 `control_update_count`：

- 连续 25 个 torque ticks 没有 controller-law update（nominal 50 ms）
  就停止 torque thread；
- thread 落入既有 `stopj(10.0)` position handoff；
- main loop 以 fault code `13` 记录 watchdog 原因。

Autotune 受控停止并释放 live writer 后，这项 repair 已随 runtime
fingerprint
`91318bee57c10f2760cfdfde8743b899021034e9471b899a60d4654ba73b10e4`
完成 fresh no-contact ordered chain。100 ms hold、0.2 mm ramp 和 2 s
reference 均观察到 Direct Torque、COMPLETE、Safety NORMAL，torque-call
分别为 `490.20 / 498.01 / 499.50 Hz`，maximum control-update gap 均为
`6 ms`。entry maximum joint excursion 分别为 `0.112 / 0.151 /
0.121 mrad`，TCP excursion 分别为 `0.076 / 0.087 / 0.055 mm`，均低于
controller fault-14 limits。

这证明 watchdog、seqlock publication 与 position-excursion guard 能在
fresh source 上共同运行；不证明 tracking、contact 或 expert-data
eligibility。

## 15. Motor telemetry 能回答什么，不能回答什么

为检查约 `0.2 Nm` command 是否在 actuator surface 上可见，runner 和
tracking analyzer 新增五类 read-only RTDE outputs：

- `target_current`；
- `actual_current`；
- `actual_current_as_torque`；
- `joint_control_output`；
- `joint_mode`。

在 fresh 2 s reference 的 `759` 个 coherent active rows 中：

- 六轴 `joint_mode` 均为 `253`；
- `joint_control_output - target_current` 的 maximum absolute difference
  为 `0`；
- commanded torque 与 `target_current` 的 centered per-joint correlation
  为 `[0.026, -0.241, -0.006, -0.004, -0.072, 0.056]`；
- `actual_current_as_torque` 的 per-joint peak-to-peak 是
  `[6.88, 10.22, 4.68, 1.88, 2.29, 1.81] Nm`，显著大于约
  `0.23 Nm` 的 command；
- calibrated `J^T w` reconstruction 与 commanded torque 在主要关节上
  仍有 `0.88--0.99` correlation，maximum commanded torque norm
  `0.229 Nm`；
- desired translation `0.629 mm`，actual max envelope `0.106 mm`，
  directional correlation `0.150`、fit gain `1.20%`，未通过 tracking
  support rule。

因此 motor telemetry 没有支持“command 已形成可辨识运动响应”，但也不能
证明 Direct Torque command 未被内部应用：这些 RTDE fields 是 motor
diagnostics，不是 Direct Torque applied-torque echo，而且
`actual_current_as_torque` 的波动会淹没当前小信号。UR internal F/T 仍未
被用作 wrench 或 guard；实验 F/T source 仍只有 Kunwei。

官方示例对 wrist 关节使用 `2.5 Nm` sinusoid，而当前 calibrated maximum
command 只有约 `0.23 Nm`。下一步合理的 single-variable isolation 是先
离线生成并验证 `K=800 N/m` 的同路径 no-contact profile，再从 fresh
compile/hold/ramp/reference ordered chain 开始；不得跳过 numeric sanity，
也不得把这轮结果直接升级为 contact 或 data collection。

这次 2 s tracking 结论与此前 2/7/10 s audit 的“response 与 noise/null
不可区分”一致，并非新发现。新增证据价值是：同一 fresh source 上确认了
seqlock coherence、watchdog、position-excursion fail-closed guard 与
motor telemetry capture，而不是重复宣称发现 weak tracking。

## 禁止回归项

- 不在 dry-run/compile probe 中调用 `direct_torque()`、`stopj()` 或 motion
  primitive；
- 不恢复 rotvec component interpolation；
- 不把 torque thread 降为 main-loop cadence；
- 不在 torque thread 内添加 `sync()`；
- 不用 stale terminal registers 代替 fresh identity handshake；
- 不把 80 ms Kunwei delivery watchdog 解释为 80 ms sample period；
- 不把 no-contact canary 数据标记为 expert/training data；
- 不把 incoherent RTDE action registers 当作 applied/expert action；
- 不把 RTDE motor-current fields 当作 UR internal F/T 或
  `direct_torque()` 的 authoritative applied-torque echo；
- 不再把 max displacement norm ratio 叫 tracking response；
- 不因 tracking 不足直接提高 torque、速度或接触力，先做单变量时间尺度
  isolation。
- 不把为根因隔离设置的全零 friction scales 当作最终 tracking 默认值；
  恢复 friction compensation 必须使用独立 profile 和完整 ordered chain。
