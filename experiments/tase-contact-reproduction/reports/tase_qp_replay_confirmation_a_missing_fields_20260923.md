# TASE-QP replay input audit: confirmation A

本次确认 A sealed attempt 的静态输入完整性已检查，但 trace 缺少逐周期 requested outer twist、`actual_dt_s`、reference clock 和 formal PATH 起点的 RNN state；封存的 `command_timeline.jsonl` 为 0 行。因此无法做公平的 matched RNN/strict Eq20 QP replay。

- Attempt: `/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/runs/tase-resident-tune-rate400-b-20260923-01/confirmation-rate400-b-v1/session-02/attempts/0001`，candidate `confirm-s02-b00-A`。
- Frozen values: Md=9.565272137974492, Bd=693.6559295653944, integral limit=1.0 N s。
- Protocol binding: `figure8_window60_r013_rate400_v1`（attempt parameter binding 与 metrics 均一致）。
- Seal verification: PASS；formal PATH published packet rows=29267；与 RTDE / sensor sequence 对上=27843 / 29267。
- Published packet rows preserve actual qdot and wrench/force registers; full RTDE rows preserve q/qd and pose/speed; raw sensor rows preserve corrected wrench. The service-observation sidecar also contains no per-row outer twist/dt/reference-time fields; `attempt-result.json` retains only its terminal provider result, not the per-tick task inputs.
- Per-sample calibrated J and static joint-position/velocity bounds were not computed by this bounded input audit; they are reconstructible from sealed q with the current calibrated model and live runtime formula. Slew-adjusted bounds remain unavailable without per-tick actual_dt_s.
- QP rejection/infeasible count, equality/bound residual, and QP/full-provider timing were not computed. No matched QP result is claimed.

下一次采集应把每个成功 PATH publish 的 sample monotonic time、actual dt、reference phase/time、requested outer twist、六维 bounds、previous successful qdot/slew scale、PATH 起点 RNN state/warm-start events 与 solver/provider timing 一起纳入 seal。

本审计只读本地封存文件；没有运行 solver、改 registry、访问网络或触碰设备。
