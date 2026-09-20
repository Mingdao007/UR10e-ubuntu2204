结论：这个 native-only scheduler change 是合理的，且在现有 hard gates 下可以接受；它可能消除 `2.1 ms` compute 后被人为推迟到下一整格、形成约 `4 ms` host gap 的额外等待，但不能把实际 `4.054/4.056 ms` 变成合格样本，也不能宣称 physical qualification。

- **数学/控制语义**：去掉 `+1` 后，若只迟到约 `0.1 ms`，下一 deadline 仍在过去，writer 会立即尝试取下一帧。这不会改变 `actual_dt_s`、runtime 的 `dt_s` 或 path clock；native provider 仍拒绝 `dt > 4 ms`，runtime 仍执行 observation-to-command deadline 和 QP deadline。这个前提与 [`yield_contact_provider.py:130-144`](/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/tools/yield_contact_provider.py:130) 和 runtime 的 measured `dt` admission 一致。

- **主要执行风险：无新帧时 busy poll**：当前 loop 明确只有 fresh RTDE 才能读 sensor、推进 state、发布 command；这是正确的。可是 deadline 仍落后时，如果 `_poll_checked()` 返回 cached/no-new frame，`sleep(max(0, next_deadline-now))` 可能连续为零，形成 tight polling，反而争抢 CPU/RTDE 时间并制造新的 gap。必须保证“立即尝试一次 fresh poll”之后仍有 bounded wait 或推进到下一个未来 deadline；不能让 no-fresh 分支无限零睡眠。

- **隔离范围**：`+1` 去除应只由 native route 的 override 生效。base `execute_attempt` 可以动态调用 `self._next_publish_deadline()`，但 base 默认实现必须保留历史行为；若直接改 base helper，会改变 legacy routes 的 timing semantics。

- **实际修复判断**：CPU2/FIFO20 的 full-loop p99 `1.88 ms`、native runtime max `1.426 ms` 支持“scheduler 人为等待”是可修复因素；但两次真实 `4.05 ms` gap 说明修复后仍必须记录并检查 host `actual_dt_s`。若 immediate fresh polling 后 gap 仍超过4 ms，应判 rate failure，转向 host scheduling/transport diagnosis；不能 clamp、round 或放宽 gate。2.1 ms injected-cost、duplicate-output、no-fresh-frame 三个 focused cases 足以验证该 change 的关键边界。

Fake stationary path-tube 命中仍只是 offline/runtime evidence，不是 physical 或 full-path qualification。
