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

下一项仍是用该 timing instrumentation 对齐 host publish、RTDE 与 TP consumed echo，找出首个延迟周期；不重跑旧 QP profile，也不在 RPSFC 工作流中改动控制器。当前归档压缩进程仍占一个 CPU 核，正式时序运行等共享 CPU/I/O lane 空出后再启动。checkpoint 的 `preflight` 和包读回只证明本次静止状态与 package 身份；它们不等于新的 live attempt 或 force-control acceptance。
