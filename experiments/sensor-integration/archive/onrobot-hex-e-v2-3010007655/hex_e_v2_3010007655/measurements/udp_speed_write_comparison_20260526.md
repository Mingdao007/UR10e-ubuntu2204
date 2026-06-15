# OnRobot UDP SPEED Write Comparison

Date: 2026-05-26

Purpose: compare open-reference UDP output after writing `SPEED=1`, then
writing back `SPEED=2`, without writing filter or bias.

| Run | Commands | Packets | Duration s | Packet Hz | Mean dt ms | Median dt ms | P99 dt ms | Sequence deltas | Sample counter deltas |
|---|---|---:|---:|---:|---:|---:|---:|---|---|
| `udp_highspeed_open_reference_20260526_204128` | `SPEED=1`, `START`, `STOP` | 997 | 1.990264937 | 500.435887 | 1.998258 | 2.002174 | 2.066509 | all `+1` | all `+2` |
| `udp_highspeed_open_reference_20260526_204143` | `SPEED=2`, `START`, `STOP` | 998 | 1.991440054 | 500.642737 | 1.997432 | 2.001983 | 2.068992 | all `+1` | all `+2` modulo `65536` |

Both runs had:

- no errors
- status `0` for every packet
- no `FILTER / 0x0081` command
- no `BIAS / 0x0042` command
- no sequence-level packet loss

Conclusion:

Both `SPEED=1` and `SPEED=2` produced an about `500 Hz` UDP packet stream in
this bench state. `SPEED=1` did not produce a `1000 Hz` UDP packet stream. This
supports treating `500 Hz` as the practical high-speed UDP output rate for the
current OnRobot HEX-E v2 / Compute Box setup.

The force-value口径 remains raw Compute Box/DAQ-like: Fz stayed around
`-28 N`, not the near-zero PolyScope variable口径.
