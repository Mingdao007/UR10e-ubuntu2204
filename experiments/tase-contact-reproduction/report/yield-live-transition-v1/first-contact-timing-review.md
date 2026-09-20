我按本小时限定范围只读检查五个目标点：readiness filter、native provider 的 `dt` 门、runtime/controller 数值步进、qualification gate，以及 writer scheduler。不会读取历史/记忆/技能，不访问硬件，不跑测试/构建，也不修改文件。先核对实现语义，再把已观察的 4.056 ms gap 与 cold-first-call 假设分开。

结论：本次是一个真实的 **host/readiness timing rejection**，不是 Protective Stop。STOP 状态90、reason4、Safety NORMAL、stationary，随后 withdrawal/Home 成功，说明安全收尾有效；qualification 仍应判失败，因为实际 readiness interval 超过了 native 合同。

- **已观察事实**：`ContactReadinessObserver.step()` 对传入的 `actual_dt_s` 执行严格 `0 < dt <= 0.004`，超出即拒绝（[`contact_benchmark_provider.py:31-40`](/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/tools/contact_benchmark_provider.py:31)）。给出的 host-use gap 是 `4.056317 ms`，所以拒绝是符合代码的。UR frame gap `3.999999986 ms` 仍在 4 ms 内，不能替 host gap 通过。

- **cold-first-call 仍只是假设**：首个 native command 在第三个 sensor-use 后约 `1.591 ms` 发布；现有时间戳没有证明 native 计算本身耗时超过 4 ms。额外的 `56 µs` 可能来自首次 native 初始化，也可能来自 sensor 到达/host scheduling；必须分段计时才能区分。

- **4 ms 的适用范围**：
  - 对 exact exponential readiness filter，它是输入合同/qualification bound，不是指数公式的数学稳定性要求；保留严格 gate 可以，但不能把它伪装成滤波器必需条件。
  - 对 numerical integration，应使用真实 `dt`，只有在控制器/solver 已证明需要 `dt≤4 ms` 时才把 4 ms 作为硬 admission bound。若采用 numerical substeps，必须保留原始 acquisition gap/rate failure。
  - 对 host arrival/publication，应分别记录并判断 sensor arrival、provider invocation、runtime step、command publish、UR frame receive；不能用 4 ms UR frame 间隔替代 4.056 ms 的 host 输入间隔。

- **prewarm 判断**：实际 instance prewarm 可能消除首次 lazy import/solver allocation，但不能证明能消除周期性 4 ms+ jitter。只有在同一 native provider、solver、数据形状和 writer 路径下重复记录 cold 与 warm 的分段耗时，且 warm 的 max/p99 都满足合同，才能把它当修复证据。

下一步最小诊断是增加一条同步时间线：`sensor_received → sensor_host_use → provider_enter → runtime_step_enter/exit → command_publish → RTDE_frame_receive`，同时记录传给 readiness/provider 的 `actual_dt_s`。若只有首次调用超时，修复 exact-path prewarm；若 warm 后仍出现 4.056 ms，修 scheduler/CPU/transport 或重新论证更宽的 timing contract。不要 clamp、round 或回填真实时间戳。
