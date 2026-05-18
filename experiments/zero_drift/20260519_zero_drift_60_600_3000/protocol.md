# UR10e Zero-Drift 60/600/3000 Protocol

目标：在末端工具已摘下、当前 payload/TCP 配置保持不变的情况下，记录一组无运动、无接触的 UR10e 力传感器零漂数据。

当前只读快照：

- Payload: `1.07 kg`
- Payload CoG: `[0.002, 0.003, 0.057] m`
- TCP offset: `[0, 0, 0, 0, 0, 0]`
- Pre-zero force snapshot: `Fx=0.312 N`, `Fy=0.634 N`, `Fz=-5.045 N`, `|F|=5.094 N`

解释：本轮不修改 payload/TCP。FT sensor 或残余安装件的质量是否已完全补偿未知，因此先记录当前真实配置下的零漂；后续如确认 payload/CoG，应另做一套 baseline。

## Segment Design

| Segment | Zeroed? | Duration | Channels | Purpose |
| --- | --- | ---: | --- | --- |
| S00_prezero_60s | No | 60 s | Fx/Fy/Fz/|F| | 清零前当前配置基线 |
| S01_rezero_60s | Yes | 60 s | Fx/Fy/Fz/|F| | 清零后短时零漂 |
| S02_rezero_600s | Yes | 600 s | Fx/Fy/Fz/|F| | 清零后中时零漂 |
| S03_rezero_3000s | Yes | 3000 s | Fx/Fy/Fz/|F| | 清零后长时零漂 |

每个 zeroed segment 前单独执行一次 `zero_ftsensor()`，等待 2 秒后采样。
