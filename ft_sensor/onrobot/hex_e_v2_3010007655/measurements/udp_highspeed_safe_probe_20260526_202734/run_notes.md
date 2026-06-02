# OnRobot UDP Safe Probe Run Notes

Date: 2026-05-26

Command:

```bash
python3 tools/onrobot_udp_safe_probe.py \
  --seconds 2 \
  --max-samples 1000 \
  --live \
  --confirm-start-stop START_STOP_ONLY
```

Safety boundary:

- Sent `START / 0x0002` with data `1000`.
- Sent final `STOP / 0x0000` with data `0`.
- Did not send `BIAS / 0x0042`.
- Did not send `FILTER / 0x0081`.
- Did not send `SPEED / 0x0082`.
- Did not send zero, TCP/payload, URScript, URCap, force-control, or robot-motion commands.

Artifacts:

- CSV: `onrobot_udp_safe_probe_20260526_202734.csv`
- Summary: `onrobot_udp_safe_probe_20260526_202734_summary.json`

Result:

- Script exit status: success.
- Errors: none.
- Status counts: `0` for all `501` received packets.
- Commands recorded in summary: exactly `START` then `STOP`.
- Received packets: `501`.
- First-last packet duration: `1.998121003 s`.
- Packet interval rate: `250.235095 Hz`.
- Mean packet interval: `3.996242 ms`.
- Median packet interval: `4.000323 ms`.
- P99 packet interval: `4.228632 ms`.
- Max packet interval: `4.412741 ms`.
- UDP sequence numbers: `1` through `501`, all deltas `+1`; no sequence-level packet loss was observed.
- Sample counter: `1528` through `3528`, all deltas `+4`; this indicates a 1000.94 Hz sample-counter tick relative to the received packet stream, not UDP packet loss by itself.
- Fz mean: `-28.782814 N`.
- Fz min/max: `-29.16 N` / `-28.47 N`.

Interpretation:

The START/STOP-only high-speed UDP route worked and produced a clean 250 Hz
packet stream in this state without sending bias, filter, or speed commands.
The device sample counter advanced by four counts per received packet, so the
current stream appears to deliver every fourth counter tick. Because the safe
probe intentionally did not set speed, this result should be labeled as the
current Compute Box streaming state after START only, not as the maximum
configured OnRobot rate.

The force-value口径 still resembles the direct TCP DAQ raw force path rather
than the near-zero PolyScope OnRobot variable口径: Fz stayed around `-29 N`.
Do not treat this as equivalent to the PolyScope variable route until the
zero/reference/compensation mismatch is resolved.

Next gate:

Do not run the open-reference client without a separate explicit approval,
because it can write OnRobot `SPEED`, `FILTER`, and `BIAS` settings.
