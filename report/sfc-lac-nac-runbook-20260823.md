# SFC/LAC/NAC 明日上机 Runbook（2026-08-23）

> 单 law、单 phase、单 admission。每一步完成后回菜单审计；不自动跨 law 串联。

## 0. 开始

```bash
sfc
```

交互式 `sfc` 会先做当前 v6 comparison package fetch/read-back，再按 binding/profile
hash 复用已接受的 tool-z orientation 与 no-contact；只有 artifact 失效或身份不匹配才
重做。菜单同时显示保留的低力 canary 和独立的 `手动交互 SFC（20 s）`。后者使用
versioned v7 interactive package；在它自己的 upload/read-back/current-stage closure
完成前必须保持禁用。任一步骤失败立即停止并保留 artifact。
`sfc --status`、`sfc --json`、
`sfc --print-menu` 仍然只读、不运动。灰色项必须保持禁用，不能绕过 blocker。

## 1. Physical setup（只做一次，共享同一 binding）

1. 保持纸盒**未填充，89.6 g**；确认夹持、线缆与工作区。
2. 在 TP 按 P0–P3 重做 Payload/CoG wizard。
3. 选择菜单 `physical setup`；分别输入 wizard UI Payload 与 CoG 数值，工具再做独立 fresh RTDE Payload/CoG/TCP read-back并比较（Payload 10 g、CoG 1 mm tolerance）。
4. 从 RG2 UI 读取当前 force；再输入 Safety Normal/Reduced Tool Speed fresh UI 数值。不得用 20 N/24 N/25 N 或旧 Tool Speed 推断。
5. 工具用同一静态 RTDE state 对更新后的 SFC/L-AC/N-AC profiles 重跑 bound numeric sanity；任一失败即停止。
6. 状态必须显示 wizard、RTDE、RG2、box state、Tool Speed 与 numeric sanity 共用同一 `physical_binding_id`。否则停止。

## 2. SFC

```text
sfc → package fetch/read-back → 复用/必要时重做 preparation → 低力（约3N）away → 退出
```

- SFC v6 bytes 保持不变，但先做 local/fetched identity check。
- `sfc` 保留 package/read-back、独立的 5 秒 `orientation settle · tool-z 水平`
  和 no-contact evidence；同一 binding/profile hash 匹配时不重复这些 preparation phase。
- 准备完成后只显示低力（约 3 N，远离 base）和退出；不要选 toward。
- 选择低力后把手放好并按一次 Enter；10 s 窗口从这个本地 ready 动作开始，避免 chat
  转发 latency 消耗等待时间。
- `await_traction` 第一帧响铃后才按提示“现在拉：远离 base”。
- 显示“已触发，继续保持”后继续保持；终端只显示两行短条、2 Hz，10 秒 timeout 仍 fail-closed。
- canary accepted 后才允许生成/使用 return admission。

### 2.1 Interactive SFC v7

```text
sfc → 手动交互 SFC（20 s）
```

- v7 basename：`sfc_box_traction_v7_interactive_100n`；当前 local candidate 不能直接 Load/Play。
- 完成 v7 package upload + fetched-back SHA/internal-content/current-stage/readiness closure 后，第一帧即进入 command-enabled SFC，固定记录 20 s。
- 不等待 traction threshold，不使用 dwell、10 s timeout、wrong-direction 或 direction-valid qualification；低力、反向和变化中的手动力全部记录为 interactive evidence。
- v7 force-norm hard stop 是 `100 N`；torque `3 Nm`、linear speed `0.08 m/s`、actual qdot `0.5 rad/s`、heartbeat、sensor/RTDE freshness、orientation、workspace/cross-axis cage 和 SIGINT/stop 保持 fail-closed。
- 开始后终端只给一次“手动交互 SFC 开始：现在拉：远离 base”提示，完整 ticks、force、qdot、TCP 和 stop reason 写入 `runs/sfc_box_traction_v7_interactive_100n/live/<run_id>/`。

## 3. L-AC

```text
lac → 状态 → package + no-contact → 状态 → canary away → 状态 → return
```

首次 `package + no-contact` 自动执行 triplet upload + fetched-back SHA closure；read-back 未闭合时不得 Load/Play 或 promotion。

## 4. N-AC

```text
nac → 状态 → package + no-contact → 状态 → canary away → 状态 → return
```

与 L-AC 相同；不得复用 L-AC anchor、admission、numeric sanity 或 no-contact。

## 5. Nominal（仅在本 law canary + return 均 accepted 后）

```text
nominal away → audit → return → nominal toward → audit → return
```

`3N/6N` 只是 `manual_exploratory` phase label；实测 force claim 只来自该 phase 的 Kunwei evidence。

## Stop conditions

任一项失败立即保留 artifact 并停止本 law 后续阶段：

- `physical_binding_id`、profile、stage、TP read-back 或 SHA 不一致；
- Remote Control / Safety NORMAL / heartbeat / timing / postflight 不闭合；
- comparison v6/L-AC/N-AC：force > 12 N、torque > 3 Nm、linear speed > 0.08 m/s、qdot > 0.5 rad/s；
- interactive SFC v7：force norm > 100 N；其余 torque/speed/qdot/heartbeat/freshness/cage hard stops 不变；
- traction direction、0.1 s dwell、10 s window、workspace/cross-axis、滑移或 return gate 失败；
- SIGINT、protective/emergency stop、sensor/RTDE stale。

报告入口：菜单 `最新报告`，或打开 `sfc-lac-nac-readiness-20260822.md`。

## 6. 2026-08-23 current-session note

- tool-z settle accepted: `runs/sfc_box_traction_v6/live/20260823T200653+0800_sfc_orientation_settle/summary.json`；姿态误差 `0.06664 → 0.02646 rad`，zero linear command，最大 TCP 漂移 `0.157 mm`。
- fresh post-settle no-contact accepted: `runs/sfc_box_traction_v6/live/20260823T200728+0800_sfc_no_contact/summary.json`；`993` samples，`495.8 Hz`，最大 command qdot `0`，最大 TCP 漂移 `0.132 mm`。
- subsequent away canary retained as failed `traction_not_detected`: `runs/sfc_box_traction_v6/live/20260823T200741+0800_operator_1267863/summary.json`；`10.0 s`，无 active samples、command qdot `0`。它只说明本次未达到持续 traction detection，不是“滑移”或“控制响应失败”结论；重试前必须重新 no-contact promotion。

### Latest logged session

本次实际交互式 `sfc` session log：
`runs/sfc_box_traction_v6/operator_sessions/20260823T202306+0800_sfc_session.log`。
自动 settle、fresh no-contact、away canary 的最终证据分别为：

```text
runs/sfc_box_traction_v6/live/20260823T202308+0800_sfc_orientation_settle/summary.json
runs/sfc_box_traction_v6/live/20260823T202439+0800_sfc_no_contact/summary.json
runs/sfc_box_traction_v6/live/20260823T202451+0800_operator_1269670/summary.json
```

最后一次 canary 是 `traction_not_detected`（dynamic threshold `1.53485 N`，无 active
sample、command qdot `0`）；操作者确认没有拉，所以不要把它解释成 motion 或滑移结论。
同目录保留 `operator.log`、`operator_events.jsonl`、`launcher_manifest.json`、
`run_envelope.json`、`evidence_manifest.json`；顶层 sidecar 还保留
`20260823T202451+0800_operator_1269670.operator.log` 和
`20260823T202451+0800_operator_1269670.operator_events.jsonl`。

最新一次低力测试（已检测到约 3.92 N，但因数值级横向 command guard 停止）的证据：

```text
runs/sfc_box_traction_v6/live/20260823T203423+0800_operator_1270440/summary.json
runs/sfc_box_traction_v6/live/20260823T203423+0800_operator_1270440/operator.log
runs/sfc_box_traction_v6/live/20260823T203423+0800_operator_1270440/operator_events.jsonl
```

整段自动流程的 session log：
`runs/sfc_box_traction_v6/operator_sessions/20260823T203320+0800_sfc_session.log`。

最新一次自动准备 session：
`runs/sfc_box_traction_v6/operator_sessions/20260823T210123+0800_sfc_session.log`；
它已完成 orientation/no-contact，但低力入口在第一次尝试中因实时周期
`2.39081 ms > 2 ms` fail-closed，保留了
`runs/sfc_box_traction_v6/live/20260823T210157+0800_operator_1272573/` 全部证据。
随后一次完整等待仍未达到持续拉力的历史证据保留在相邻 `traction_not_detected` run 中；
这两次都没有形成 command-enabled motion。
