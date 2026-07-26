# OnRobot UDP Open Reference Run Notes: SPEED=1

Date: 2026-05-26

Command:

```bash
python3 tools/onrobot_udp_open_reference.py \
  --profile custom \
  --speed-divisor 1 \
  --seconds 2 \
  --max-samples 2000 \
  --live \
  --allow-config-writes OPEN_ONROBOT_UDP_CONFIG
```

Approval scope:

- Write `SPEED / 0x0082 = 1`.
- Send `START / 0x0002` with data `2000`.
- Send final `STOP / 0x0000`.
- Do not send `FILTER / 0x0081`.
- Do not send `BIAS / 0x0042`.

Artifacts:

- CSV: `onrobot_udp_open_reference_20260526_204128.csv`
- Summary: `onrobot_udp_open_reference_20260526_204128_summary.json`

Result:

- Script exit status: success.
- Errors: none.
- Status counts: `0` for all `997` received packets.
- Commands recorded in summary: exactly `SPEED=1`, `START`, `STOP`.
- Received packets: `997`.
- First-last packet duration: `1.990264937 s`.
- Packet interval rate: `500.435887 Hz`.
- Mean packet interval: `1.998258 ms`.
- Median packet interval: `2.002174 ms`.
- P99 packet interval: `2.066509 ms`.
- Max packet interval: `3.051051 ms`.
- UDP sequence numbers: all deltas `+1`; no sequence-level packet loss was observed.
- Sample counter: all deltas `+2`.
- Sample-counter tick rate from deltas: about `1000.871775 Hz`.
- Fz mean: `-28.457693 N`.
- Fz min/max: `-28.81 N` / `-28.10 N`.

Interpretation:

`SPEED=1` did not produce a 1000 Hz UDP packet stream. It produced an about
500 Hz UDP packet stream, with the sample counter advancing by two counts per
received packet. This is consistent with the HEX-E datasheet-level `500 Hz`
maximum sampling frequency and suggests the device/firmware clamps or otherwise
limits packet output near 500 Hz.

This run did not write filter or bias settings.
