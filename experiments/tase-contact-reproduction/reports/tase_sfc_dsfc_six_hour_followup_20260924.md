# TASE 接触验收与 SFC/DSFC 离线比较

日期：2026-09-24。实验入口位于 `experiments/tase-contact-reproduction`。本报告合并短接触阶梯、A 参数实机工程验收、已封存的 rate400 积分/autotuner 结果，以及 TASE+SFC/DSFC 离线实验。所有数值保留原始 run 身份；模型结果、Kunwei 反馈诊断和实机结果分开。

## 结论

- 独立短接触入口完成了 8、4、3、2、1 秒 ramp，并把 1 秒档重复两次。七次都满足现有 0.5 秒接触放行条件并自动回 joint Home；1 秒是最快通过档。该入口有独立 10 N force-norm 停试线，实测峰值 7.01–8.47 N。它是短接触资格数据，不是 Figure-eight 或 autotuner 数据。
- 冻结 A 参数的四圈工程验收没有完成：尝试在正式 PATH 前因 5 N 接触放行窗口不稳定而失败。另一轮在运动前被 observer 新鲜度门槛拦住。**完整 60 秒 PATH 为 0/4，故无四圈真实周转中位数，也不能宣称 ≤81 秒。** 失败记录保留，两个实际运动故障都由 recovery owner 自动回到 Home。
- 同条件 SFC/DSFC 比较完成 30/30 个模型代理单元，另完成 30 次固定输入命令重放。它们支持代码路径和仿真诊断，不支持实机优胜或真实 task-force 结论。
- 最新只读 bench 检查确认机械臂位于批准 joint Home、静止、Safety NORMAL、Dashboard STOPPED，且没有 active writer。尚未完成四圈实机门槛，因此本 goal 保持未完成。

## 短接触阶梯

证据目录：`runs/contact-ramp-probe-ladder-20260924-r2-retry3/ladder-progress.json`。每档用独立 probe 包；目标为 5 N，放行要求连续满足 0.5 秒，目标后最多观察 2 秒，force norm 达 10 N 停止。每次通过后自动 joint Home 才开始下一档。

| 标称 ramp | 从 ramp 开始到放行 (s) | 达到 5 N 后稳定等待 (s) | force-norm 峰值 (N) | Home |
|---:|---:|---:|---:|---|
| 8 s | 8.598 | 0.598 | 7.515 | 通过 |
| 4 s | 4.600 | 0.600 | 7.699 | 通过 |
| 3 s | 3.600 | 0.600 | 8.467 | 通过 |
| 2 s | 2.600 | 0.600 | 7.845 | 通过 |
| 1 s | 1.964 | 0.964 | 7.375 | 通过 |
| 1 s 重复 1 | 1.958 | 0.958 | 7.011 | 通过 |
| 1 s 重复 2 | 1.988 | 0.988 | 8.039 | 通过 |

最快档 1 秒两次重复均通过，已按计划恢复常规生产 TP 包并完成包 read-back；restore receipt 显示 `step5d_contact_six_qp_v1.urp` 已加载但保持 STOPPED。probe 包没有被保留为生产控制器。短接触力值来自 Kunwei 校正反馈和 RTDE，不是独立 task-force 真值。

本轮 probe 目录中没有编码视频文件，保存的 `video-stderr.txt` 表示录制源不可用；这里仅说明这些具体 run 的证据状态，不推断其他视频源当前不可用。同步 RTDE、Kunwei、TP 和 Home receipt 已保存。

需要注意：8 秒 probe 是独立固定 ramp primitive；后续 8 秒 TASE 正式入口使用另一个控制路径。probe 的通过不能证明 TASE 正式入口的接触窗口稳定。

## TASE A 参数的四圈工程验收

计划协议为 `TASE_RNN_MATURE`、`r013_60_rate400`、60 秒 PATH、正式窗口 `[5,60)`、目标 5 N、8 秒升力；固定 A 参数为 `Md=9.565272137974492`、`Bd=693.6559295653944`、积分上限 `1.0 N·s`。运行结果确认这些参数确实传入 runtime。

| 记录 | 发生情况 | 结果 |
|---|---|---|
| `runs/tase-a-four-unit-engineering-20260924T0515Z/` | 实际开始接触；provider 在 release gate 等待时未形成足够稳定的放行窗口，TP state 21 到 30 s watchdog 后停止，未进入 PATH | recovery owner 自动 Home；attempt 失败 |
| `runs/tase-a-four-unit-engineering-fixed-20260924T0548HKT/` | 起始准备的 observer 样本已过期，未 ARM、未发生机械臂运动 | 不是物理试验单元；安全留在/恢复至 Home |
| `runs/tase-a-four-unit-engineering-fixed-20260924T0600HKT/attempts/0001/` | sensor 和 RTDE 样本均 fresh；state 21 接触等待 30.008 s 后 reason 48。最长连续联合放行窗口 0.4356 s，小于要求的 0.5 s；filtered normal 4–5.5 N 占 64.97%，raw normal 3–7 N 占 74.71%，torque/freshness 条件占 100% | 未开始正式 PATH。force-norm 峰值 10.526 N 是诊断值；该次不是短 probe 的 10 N 停试协议。recovery owner 自动 Home |

失败先发生在**接触放行**：力反馈振荡/低谷不断重置联合计时。state 21 停留 30 秒是 watchdog 超时，不是完整 PATH，也不能计入成功圈。前一次失败曾表现为 release gate 期间 TASE 输出停滞；后续 observer 新鲜度修复使 06:00 轮进入实际接触，但并未使接触 gate 通过。当前证据说明修掉 observer/零输出问题仍不足以解决接触稳定性。

因此本轮冷启动加三次 resident 复用的四个完整单元没有完成，完整 PATH 计数为 0/4；≤81 秒普通复用周转目标无可计算结果。没有按同一失败原因重复继续派发实机尝试。应先用这批 raw trace 定位接触阶段的输出与传感时间关系；若改用已经通过的 1 秒 ramp，必须建立新的、明确独立的 Figure-eight 协议身份，不能追认到原 8 秒协议或拿 probe 结果代替 PATH。

## rate400 历史结果：保留、不重跑

这些结果属于前一批已封存的 `figure8_window60_r013_rate400_v1`，不属于本轮 8 秒接触验收。

- 五策略积分筛选 25/25 个预算单元：A 4/5 完整、B 5/5、C 3/5、D 5/5、E 3/5。按“完整次数优先、完整样本 MAE 次之”选出 B；失败单元保留在分母。
- B 策略自动调参 24 次：22 完整、2 失败；最好候选为 `Md=8.592659656558919`、`Bd=772.1473715259434`。冻结候选的五轮有效配对确认比较 incumbent+A 与 tuned+B；一次额外 B 尝试因 evidence-ineligible 保留为失败，五个有效配对仍齐全。
- 配对平均改善 `0.0347 N`，95% CI `[-0.1767, 0.2461] N`，未支持改善，也没有达到预设 0.10 N 的实际改善标准。后续以 A / incumbent `Md=9.565272137974492`、`Bd=693.6559295653944` 为基线；无需重跑这笔预算。
- 这些 MAE 是传感器反馈的 normal-force 指标，不是独立 task-force 真值。B 在筛选中胜出仅是预算内策略选择，不等于统计确认优于 A。

来源：`runs/tase-integral-ABCDE-screening-20260923-rate400/screening-results.json`、`runs/tase-resident-tune-rate400-b-20260923-01/summary.json` 和 `runs/tase-resident-tune-rate400-b-20260923-01/confirmation-rate400-b-v1/summary.json`。

## TASE + SFC / DSFC 离线比较

来源：`runs/tase-sfc-dsfc-offline-20260924T0558HKT/summary.json`、`report.md`、`command-replay.json`、30 条压缩 trace。正常力/姿态使用 TASE-RNN Mature 与 A 参数；切向分别为 `SFC_YIELD_V1` 和 `DSFC_YIELD_V1`。两者共用 normal observer、轨迹、参数、5 N 目标、120 N/m 路径项、0.002 s 离散步长、±0.05 rad/s joint 边界。切向指令先投影再组合，最终 joint-velocity realization 每周期只调用一次。

30 个模型代理单元（5 block × 3 场景 × 2 controller）全部完成。0.6 N 合成扰动不是实机推扰，以下 MAE 是**模型预测**：

| 场景 | SFC MAE (N) | DSFC MAE (N) | DSFC−SFC (N) | 路径 RMS：SFC / DSFC (m) |
|---|---:|---:|---:|---:|
| 平面 | 0.0001671 | 0.0001677 | +0.0000006 | 0.0000727 / 0.0000231 |
| 切向脉冲 | 0.0014093 | 0.0016751 | +0.0002658 | 0.0024658 / 0.0024752 |
| 法向脉冲 | 0.1021388 | 0.1102164 | +0.0080776 | 0.0160534 / 0.0153607 |

简化模型使用 identity Jacobian。切向脉冲 joint-bound 命中约为 SFC 21.39%、DSFC 20.24%；法向脉冲均为 64.24%。这说明代理已较多处于速度边界，数据不足以排序真实力控优劣。仿真中 SFC MAE 较低是描述性结果，不是实机改善结论。

同输入命令 replay 使用 15 条固定模型输入轨迹，每条分别回放两个 law，共 30 次；配对输入 digest 一致，切向泄漏至估计法向的最大值 `8.44e-19 m/s`，最终 joint realization 调用数为 `[1]`。它是模型 trace 的 command counterfactual，不是 UR 历史 trace 重放，也不是 live replay。

历史 TASE trace 审计了 45 个目录中的 148 个 raw sensor、148 个 RTDE、148 个 packet stream 的首条非空 schema。缺少控制器实际消费字段：estimated normal、force target、Jacobian、joint bounds、raw normal force、参考位置/速度、state age、TCP position。故历史实机 trace 不满足替代 controller 的 byte-identical input replay 条件；补算这些字段会变成重建输入。

## 关节＋末端双空间离线意图

这部分的 DSFC law 和 dual-space collision policy 是两个不同模块。当前碰撞策略仅有 `offline_intent_only` 权限：工具碰撞夹具里冻结切向意图并保持 TASE normal/orientation；合成 link-contact observer 夹具可提出关节让步意图；工具超出合成 corridor 会请求唯一 recovery owner。法向与姿态误差约 `2.07e-19 m/s`、`0 rad/s`。

该结果只验证接口意图。当前没有合格的连杆外力观测器、独立施力真值、标定图像工件 corridor 或实际装置评分 trial；本轮也没有装置或人工推扰。真实机械臂 SFC/DSFC 资格和工件标记改善都未验证。所提“交互力下降”和“避免末端运动给工件留下额外划痕”仍是两个独立设计目标；速度/误差依赖的 damping 关系及其增益仍未获决定，本报告不替用户指定。

## 验证、当前状态和下一步

相关测试结果：本次合并 focused regression **142 passed**，覆盖 qualification/provider、recovery、supervisor、transport、ramp probe、R006、SFC/DSFC 和 dual-space；板面标记检测另有 **6 passed**。`git diff --cached --check` 通过。离线 CPU preflight `scripts/contact-six.sh status` 为 `ok=true`，`device_io=false`，`motion_authorized=false`。

2026-09-24 06:41 HKT 的 fresh read-only hardware preflight：`actual_q=[0.74520820,-1.81808819,-2.56270456,-0.31234105,1.52763081,-0.82386190] rad`，相对批准 joint Home 最大误差约 `7.0e-5 rad`；TCP 线速度为 0，Safety NORMAL，Dashboard `STOPPED step5d_contact_home_v1.urp`，Kunwei TCP connect-only 成功，active writer 数为 0。此检查无 motion、无传感器控制命令。

下一项物理工作不应直接开四圈重复旧配置：先基于 06:00 state 21 的 raw sensor、RTDE 和 published packet 时间线定位 release gate 未连续通过 0.5 秒的原因，并与 1 秒 probe 的固定 ramp primitive 做代码级差异对照。然后由独立协议决定是否用 1 秒接触建立流程进入完整 60 秒 PATH；此前四圈基建门槛仍未通过。
