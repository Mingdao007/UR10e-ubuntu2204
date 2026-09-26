# TASE 上电续接检查点（2026-09-26）

## 现场状态

用户要求“power on 然后继续”。19:52 HKT 的 fresh read-only 检查显示机器人已经上电并处于 `RUNNING`，因此没有重复发送 power-on 或 brake-release。Dashboard 为 Remote、Safety `NORMAL`，`step5d_contact_six_qp_v1.urp` 为 `STOPPED`；本检查点没有发出 Load/Play、URScript、RTDE 运动指令、Kunwei 写命令或传感器 zero/tare。

RTDE 实测 TCP 速度为零；相对于批准 Figure-eight/Autotuner joint Home，`actual_q` 最大误差为 `4.73e-5 rad`。短末端读回 payload `0.413 kg`、CoG `[0.0011, 0.0031, 0.0163] m`、TCP z `87.4 mm`。当前一次 RTDE `actual_TCP_force` norm 为 `0.654 N`，这只是 UR 内部诊断量，不是 KWR75 的独立接触力证据。

当前控制器程序从 `/programs/andyl/kunwei/step5` fresh fetch 的 `.script/.txt/.urp` 三项 SHA 均与 R31 清单一致。OctoStream `/arm` 可读取；封存了一张当时画面，显示短末端位于板面上方且有可见间隙。该文件只是单帧观察，不代表连续录像。Kunwei 本次只做 TCP connect-only 检查，没有采样、更没有写入或清零。

完整机器字段和原始读回文件见 [resume checkpoint](../runs/tase-resume-readonly-20260926T1950HKT/checkpoint.json)、[preflight](../runs/tase-resume-readonly-20260926T1950HKT/fresh-preflight/preflight_readonly.json)、[controller read-back](../runs/tase-resume-readonly-20260926T1950HKT/controller-readback-r31-fresh/) 和 [OctoStream 单帧](../runs/tase-resume-readonly-20260926T1950HKT/octostream-observer-frame.png)。下一次 live attempt 前仍须重新采 KWR75 no-contact baseline，并在 observer barrier 启动连续录像。

## RPSFC 与 TASE 的证据边界

短末端 RPSFC `rho=0.1 N` 的 20 s 无接触实机运行已经完成：最大 TCP 三维位移 `0.495 mm`、最终位移 `0.434 mm`，10 mm stop 未触发，自动回到同一 Autotuner joint Home，视频和 raw trace 已封存。单次实机运行说明在该次噪声下漂移小于 0.5 mm；它没有与 live `rho=1 N` 做配对，不能称为改善。

按同一留存静止力序列作的离线状态重放显示，`rho=1 N` 参考始终休止；`rho=0.1 N` 有约一半瞬时样本越出静止力球，并产生小幅 law-only 命令路径。因此现有证据不支持 100 mN 改善噪声下的慢漂，降低阈值反而让控制器对更多噪声作出响应。低于 100 mN 的人手轻推仍可能被忽略，实际检测能力没有推扰真值验证。详见独立的 [RPSFC 报告](</home/andy/.codex-worktrees/short-eoat-sfc-dsfc-drift-20260924/reports/short_eoat_rpsfc_100mn_20260925.md>)；这些结果不并入 TASE 接触力 MAE。

## TASE 续接位置

目前有效的 TASE-RNN A 完整圈仍是 R32：`r013_60_rate460`、5 N、60 s、正式窗口 `[5,60)`、550/550 bins、`Md=9.565272137974492`、`Bd=693.6559295653944`、积分上限 `1.0 N·s`，MAE `1.098445 N`，joint Home verified。对应的分层速率为 writer `499.683 Hz`、RTDE `500.000 Hz`、TP consumed `480.936 Hz`；该圈 timing gate 合格。

时序诊断新增一个 focused 修复：连续 qualification 成功后不能把已满足的 3 次 baseline 计数降成 1，否则 legacy candidate 会正确 fail-closed 为 `baseline_qualification_missing`，使后续时序 marker 没有机会出现。现在只把计数提升到至少 1、保留已有更强证据；R006 当前单次资格规则不变。七个 writer/supervisor 生命周期测试文件共 **101 passed**。修复提交为 `29796f5f`（尚未 push）。

R32 receipt 实际已经带有 host hot-path、publish/send、provider 与 RTDE-poll 的分层摘要；此前“没有此次逐阶段计时”的描述已在决策树更正。现有 raw join 已把 `sequence 36485→36486` 的最大 host interpublish `11.891 ms` / `actual_dt=12.068 ms` 定位到 host observation/control-loop gap：RTDE poll 相隔 `12.059 ms` 后恢复，并一次 drain 六帧，其 controller timestamp 连续保持 2 ms 间隔。provider `1.044 ms`、solver `0.091 ms`、publish latency `1.654 ms` 都不足以说明这段等待；具体未计时 wait/scheduler/call boundary 仍未知，不能直接归因为 network 或 Linux scheduler。

## 20:11 HKT fresh read-only 续接

新 preflight 位于 [20:11 fresh preflight](../runs/tase-resume-readonly-20260926T2012HKT/fresh-preflight/preflight_readonly.json)，时间 `2026-09-26T20:11:50+08:00`。Dashboard/RTDE 再次显示机器人已上电 (`Robotmode RUNNING`)、Remote、Safety `NORMAL`、程序 `STOPPED`，零 TCP 速度；没有发送 power-on、brake-release、Load/Play 或任何运动/传感器写命令。实际关节对批准 Home 的最大差值约 `8.1e-5 rad`；TCP z `87.4 mm`、payload `0.413 kg`、CoG `[0.0011, 0.0031, 0.0163] m`。Kunwei 仅 TCP connect-only；该 preflight 没有取样或 zero/tare。一次 UR `actual_TCP_force` norm 为 `0.202 N`，仅作诊断，不能独立证明 no-contact。

此时没有 live writer；旧 global lock 文件记载 PID `1011853`，该 PID 已不存在，不能当作仍持锁的进程。另有无关 archive compaction PID `1012539` 持续使用一个 CPU 核并写外部盘。保留它的运行控制权；正式时序采集等共享资源 lane 空出后再执行。上次 OctoStream 证据仍是 19:50 的单帧，不替代下一次 live attempt 前的连续 observer barrier；fresh KWR75 no-contact baseline 也仍待采。

## 积分筛选与 autotuner 入口恢复

核对发现 rate460 五策略筛选早已完成：25次全部封存，20圈完整、5圈失败。A/B/C/D/E 的完整次数分别为 `4/3/4/4/5`，成功圈 MAE 均值分别为 `0.977/1.140/1.756/1.235/1.929 N`。预定的“完成次数优先”规则选E；E的力误差均值最高、A最低，因此不能把E描述成力控最优。四个 rate失败跨 A/B/C/D，writer/RTDE/TP 速率一致而 Kunwei仍约1000 Hz；host publish p95间隔 `2.617–4.496 ms`、最大 `7.265–10.577 ms`。已测 provider/solver/publish 子段都短于2 ms，但旧失败圈没有逐阶段计时，仍未知道主机循环具体在哪段等待。screen-D-00 实际绑定 conditional double clamp，sealed timeline 的已发布输出反馈字段覆盖该圈29,994/29,994行。

阈值反事实：若只把 frequency floor 从460降到400Hz，上述四个 rate-only失败中三次会过频率门槛，`380.15 Hz`仍不通过；这不是独立 `rate400` 协议的验收或配对证据。正式 TASE 当前仍按 rate460 profile 计量。

旧 `figure8.sh --autotune` 把配置固定成A，无法跟随已选E；现已恢复单一入口：默认使用筛选所选E配置，A可由显式配置和A基线receipt启动，resume 默认读取 campaign 的冻结配置。入口 focused tests **24 passed**，E的 initial-00 也通过现有 sealed-screening source 校验。正式24次 tuning 尚未启动。报告索引见[研究决策树](tase_control_research_decision_tree_20260926.md)；源结果见[screening result](../runs/tase-r31-integral-screening-20260925T1825HKT/screening-results.json)。
