# OnRobot UDP Open Reference Run Notes: SPEED=2

Date: 2026-05-26

Command:

```bash
python3 tools/onrobot_udp_open_reference.py \
  --profile custom \
  --speed-divisor 2 \
  --seconds 2 \
  --max-samples 1000 \
  --live \
  --allow-config-writes OPEN_ONROBOT_UDP_CONFIG
```

Approval scope:

- Write `SPEED / 0x0082 = 2` after the `SPEED=1` test.
- Send `START / 0x0002` with data `1000`.
- Send final `STOP / 0x0000`.
- Do not send `FILTER / 0x0081`.
- Do not send `BIAS / 0x0042`.

Artifacts:

- CSV: `onrobot_udp_open_reference_20260526_204143.csv`
- Summary: `onrobot_udp_open_reference_20260526_204143_summary.json`

Result:

- Script exit status: success.
- Errors: none.
- Status counts: `0` for all `998` received packets.
- Commands recorded in summary: exactly `SPEED=2`, `START`, `STOP`.
- Received packets: `998`.
- First-last packet duration: `1.991440054 s`.
- Packet interval rate: `500.642737 Hz`.
- Mean packet interval: `1.997432 ms`.
- Median packet interval: `2.001983 ms`.
- P99 packet interval: `2.068992 ms`.
- Max packet interval: `2.611378 ms`.
- UDP sequence numbers: all deltas `+1`; no sequence-level packet loss was observed.
- Sample counter modulo `65536`: all deltas `+2`.
- Sample-counter tick rate from modulo-65536 deltas: about `1001.285475 Hz`.
- Fz mean: `-28.274379 N`.
- Fz min/max: `-28.62 N` / `-27.98 N`.

Interpretation:

Writing `SPEED=2` after the `SPEED=1` run preserved the about 500 Hz UDP packet
stream. The summary JSON reports one raw sample-counter backward step because
the counter wrapped from the high `63xxx` range to `156`; modulo `65536`, the
counter advanced by `+2` for every received packet. This wrap is not UDP packet
loss.

This run did not write filter or bias settings.
