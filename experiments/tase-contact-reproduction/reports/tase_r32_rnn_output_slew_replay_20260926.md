# R32 TASE-RNN 输出与 host slew replay

日期：2026-09-26（HKT）

身份：`TASE_RNN_MATURE`、`figure8_window60_r013_rate460_v1`、`LEGACY_R1`。可信度：sealed live trace 的离线 source-equation replay；不是新的实机试验。

## 结果

对 R32 A baseline 的 **29,989** 个 sealed PATH tick 逐帧重放后，先前看到的 11 个 `published qdot` 差异全部来自 host slew 的公式被分析脚本写错。运行代码不是把整个 qdot 乘比例，而是从上一帧发布值出发，只按比例走完本帧的增量：

```text
qdot_sent = qdot_previous + scale * (qdot_rnn - qdot_previous)
```

29,978 帧的 RNN 输出本身直接匹配已发布 qdot；另外 11 帧全部带 `host_slew_limited=true`。按上式重放后 **29,989/29,989 帧匹配**，最大误差 `8.67e-19 rad/s`。错误的 `scale * qdot_rnn` 算法在同 11 帧产生最大 `0.002060 rad/s` 偏差。第一处差异在 PATH `t=0.134 s`，属于 slew 限幅帧。

因此，这 11 帧不是新的 RNN 输出包络或 provider 不一致证据；复现差异由离线分析误用了缩放公式造成。现有证据不支持为此改控制器，也不说明 slew 对力 MAE 的因果影响。

重放输入是 [R32 A baseline attempt](../runs/tase-r32-rate460-a-v3-capture-evidence-only-20260926T171700HKT/attempts/0001/) 的 sealed `command_timeline.jsonl`，SHA-256：`c296074b4a583709a9fe810d6680bd7a39f515849c932b4a2ba6c8967c8975ee`。可复算结果在 [machine-readable replay](tase_r32_rnn_output_slew_replay_20260926.json)，复算入口在 [replay_tase_r32_rnn_output_slew.py](../tools/replay_tase_r32_rnn_output_slew.py)。Focused tests：3 passed。

该 R32 圈的正式 `[5,60)` MAE 是 **1.098445 N**，550/550 bins；同次 `[0,60)` feedback-sample MAE 是 **1.126476 N**。它是 Kunwei 控制器反馈，不是独立 task-force 真值，且本报告只复核 qdot 命令重放，不重算力指标。`step5c_tase_paper_truth.json` 的论文核验字段仍未清完，所以 source-equation replay 不提升论文复现结论。

## 本次上电与 no-motion 检查

机器人在本轮第一次 fresh Dashboard 读取时已经是 `RUNNING`、`Safety NORMAL`、Remote Control=true，程序 `STOPPED`，且 RTDE 位于批准 Figure-eight joint Home 附近；因此没有再发送 `power on` 命令。其后 fresh preparation 重新 GET 并校验 contact/Home 两个 TP package，采到 4,746 个 Kunwei 静止样本；这是软件 baseline，未执行传感器硬件 tare。OctoStream 主视频可用。baseline 录影曾停止推进，但 `evidence-only` 下保留该缺口，RTDE/Kunwei 主观察仍完整。

本次 `resident-start-check` 只执行了 Dashboard Load/Play/Stop：驻留 10 秒、收到 1,000 个 RTDE 帧，`motion_commands_from_supervisor=0`、`arm_dispatched=false`、`path_dispatched=false`。最终 Dashboard 为 `STOPPED`、Safety `NORMAL`，关节 Home 最大误差 `4.73e-5 rad`。证据保存在 [resident-start-check run](../runs/tase-powered-resident-start-check-20260926T1919HKT/)。这证明 resident no-motion start-check 成功，**不等于新的 60 秒力控单元或 TASE-QP 实机试验**。

Fresh RTDE identity 为 payload `0.413 kg`、CoG `[0.0011, 0.0031, 0.0163] m`、TCP z `0.0874 m`。主录影长 `14.831 s` 且可读；一帧可见末端位于平面试件上方，但这张图不能量化整段轨迹的最小净空。Preparation 的另一段 baseline video 曾报告停止推进，已保留为 capture 缺口；不影响本次 read-back、RTDE/Kunwei baseline 或 resident idle 的结论。

## 下一步

R31 两次 TASE-QP 的完整 60 秒路径虽然完成，实际关节速度相关门槛都失败（0.840132、0.848607，门槛 0.900）；固定 QP profile、门槛和 slew 不变，不以重复同一试验消耗实机预算。按已完成的 [R32 同输入 SFC/DSFC replay](tase_r32_live_trace_controller_replay_20260926.json) 继续离线核对 SFC+TASE 的法向/切向组合；该重放是 open-loop counterfactual，不是闭环力控或实机改善证据。进入下一次正式时序 live run 前，先确认共享 CPU/I/O 资源空闲。
