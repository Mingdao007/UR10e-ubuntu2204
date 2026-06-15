# OnRobot High-Speed UDP Probe Scripts

These scripts target the OnRobot Ethernet DAQ high-speed UDP surface on
`192.168.1.1:49152`.

They are prepared for offline review first. Do not run either script live on
the bench without an explicit operator approval step.

## Safer START/STOP-Only Probe

Path:

```text
tools/onrobot_udp_safe_probe.py
```

Default behavior is dry-run only:

```bash
python3 tools/onrobot_udp_safe_probe.py
```

Live execution requires an explicit gate:

```bash
python3 tools/onrobot_udp_safe_probe.py \
  --seconds 5 \
  --max-samples 2500 \
  --live \
  --confirm-start-stop START_STOP_ONLY
```

Safety boundary:

- sends `START / 0x0002`
- attempts final `STOP / 0x0000`
- does not expose or send `BIAS / 0x0042`
- does not expose or send `FILTER / 0x0081`
- does not expose or send `SPEED / 0x0082`

This is still not passive read-only because START/STOP changes the Compute Box
streaming state.

## Open-Permission Reference Client

Path:

```text
tools/onrobot_udp_open_reference.py
```

Default behavior is dry-run only. The default `vendor-demo` profile mirrors the
vendor C sample plan: `SPEED 10`, `FILTER 4`, `BIAS off`, `START`, mid-run
`BIAS on`, final `STOP`.

```bash
python3 tools/onrobot_udp_open_reference.py
```

Live execution requires an explicit configuration-write gate:

```bash
python3 tools/onrobot_udp_open_reference.py \
  --profile vendor-demo \
  --seconds 5 \
  --max-samples 2500 \
  --live \
  --allow-config-writes OPEN_ONROBOT_UDP_CONFIG
```

Use `--profile custom` when choosing exact commands:

```bash
python3 tools/onrobot_udp_open_reference.py \
  --profile custom \
  --speed-divisor 2 \
  --filter-mode 0 \
  --initial-bias off \
  --midrun-bias none
```

The open client can write OnRobot speed, filter, and bias settings. Keep it
separate from the safer probe.

## Outputs

Live runs write a CSV and summary JSON under a timestamped directory in:

```text
measurements/
```

The CSV contains sequence number, sample counter, status, raw six-axis values,
and scaled force/torque using the vendor high-speed UDP example divisors:

- force: raw / `10000.0`
- torque: raw / `100000.0`

The summary includes timing, sequence/sample-counter gap estimates, status
counts, command log, scaling, and force/torque min/mean/max fields.
