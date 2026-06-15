# UR10e Zero-Drift Retest Protocol: Payload 0.009 kg

目标：在 payload 已从 `1.07 kg` 改为 `0.009 kg`、CoG/TCP 为 0 后，重新测 60/600/1800 秒力漂移。

当前只读快照：

- Payload: `0.009 kg`
- Payload CoG: `[0, 0, 0] m`
- TCP offset: `[0, 0, 0, 0, 0, 0]`
- Pre-zero force snapshot: `Fx=-0.926 N`, `Fy=-0.763 N`, `Fz=-1.178 N`, `|F|=1.682 N`

## Segment Design

| Segment | Zeroed? | Duration | Channels | Purpose |
| --- | --- | ---: | --- | --- |
| S00_prezero_60s | No | 60 s | Fx/Fy/Fz/|F| | 清零前当前配置基线 |
| S01_rezero_60s | Yes | 60 s | Fx/Fy/Fz/|F| | payload 修正后短时零漂 |
| S02_rezero_600s | Yes | 600 s | Fx/Fy/Fz/|F| | payload 修正后中时零漂 |
| S03_rezero_1800s | Yes | 1800 s | Fx/Fy/Fz/|F| | payload 修正后长时零漂 |

每个 zeroed segment 前单独执行一次 `zero_ftsensor()`，等待 2 秒后采样。
