# Direct Torque v4 真机经验汇总

本文是 TacDiffusion native Remote Direct Torque v4 的单一经验入口。动态
stage 状态仍以 `STEP5D_TACDIFFUSION_FLOW.md` 和
`config/step5_stage_table.json` 为准；本文件集中记录已经由代码、A/B
probe 或真机数据支持的可复用经验。

## 当前结论

- 2026-07-28 的最新 ordered no-contact chain 已通过：
  `hold_100ms -> ramp_0_2mm_500ms -> reference_2s`。
- 同一 runtime fingerprint 随后通过了 diagnostic-only
  `reference_10s_diagnostic`；该 stage 不改变 canonical acceptance。
- 该 chain 证明 receiver transport、identity handshake、Direct Torque
  有界执行、controller/host cadence、Kunwei capture 和安全退出。
- 它不证明 trajectory fidelity、contact control 或 expert-data
  eligibility；所有现有 capture 均为 `training_dataset=false`。
- 当前冻结 receiver source SHA：
  `86545a209b348b9f19469239686f778a7b9ff19c065f771d73f2c3310a85e67f`。

最新主要证据：

- `runs/tacdiffusion/direct_torque_v4_reference10s_chain_hold_20260728T1311HKT/evidence.json`
- `runs/tacdiffusion/direct_torque_v4_reference10s_chain_ramp_20260728T1312HKT/evidence.json`
- `runs/tacdiffusion/direct_torque_v4_reference10s_chain_reference2s_20260728T1313HKT/evidence.json`
- `runs/tacdiffusion/direct_torque_v4_reference_10s_diagnostic_20260728T1314HKT/evidence.json`
- `runs/tacdiffusion/direct_torque_v4_reference_10s_diagnostic_20260728T1314HKT/reference_10s_tracking_audit_v1.json`

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
解决更长时间 tracking。操作者报告 recent official-friction canary 均有
可听声音；3 s 被认为不足，7 s 已采集，声音究竟持续全程还是集中在
entry/exit 仍需操作者按时间分类。没有 audio capture 时，不得仅凭
RTDE 数值把声音归因给 acceleration、tracking 或 friction model。

## 禁止回归项

- 不在 dry-run/compile probe 中调用 `direct_torque()`、`stopj()` 或 motion
  primitive；
- 不恢复 rotvec component interpolation；
- 不把 torque thread 降为 main-loop cadence；
- 不在 torque thread 内添加 `sync()`；
- 不用 stale terminal registers 代替 fresh identity handshake；
- 不把 80 ms Kunwei delivery watchdog 解释为 80 ms sample period；
- 不把 no-contact canary 数据标记为 expert/training data；
- 不因 tracking 不足直接提高 torque、速度或接触力，先做单变量时间尺度
  isolation。
- 不把为根因隔离设置的全零 friction scales 当作最终 tracking 默认值；
  恢复 friction compensation 必须使用独立 profile 和完整 ordered chain。
