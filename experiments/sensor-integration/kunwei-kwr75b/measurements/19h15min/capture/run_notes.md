# Kunwei KWR75B 1 kHz 24h Capture

Started: 2026-06-02 19:57:27 +08:00

Planned duration: 24 h, unless manually interrupted.

tmux session:

`kunwei_24h_20260602`

Interrupt command:

`tmux send-keys -t kunwei_24h_20260602 C-c`

## Manual Basis

- `0x48 AA 0D 0A` starts converted-result streaming at 1 kHz.
- `0x43 AA 0D 0A` stops data conversion and sending.
- Data frames are 28 bytes: `0x48/0x49`, `0xAA`, six float32 components, `0x0D 0x0A`.
- Raw manual units are `Kg` for force and `Kg*m` for moment.
- No `EE AA` configuration write was sent.

## Network Path

Manual default routes checked first:

- TCP listen on host `192.168.50.26:8886`: no incoming connection in the short probe.
- TCP client to `192.168.50.25:8886`: connection refused.
- UDP command/listen on `8886`: no data frames received.

Observed working data route:

- TCP client to `192.168.50.25:5152`
- Short probe on this route captured `7985` valid `0x48` frames in about 8 s.
- Official metadata records that TCP `5152` is used only as the observed data socket for `0x48/0x43`; UDP `5152/5153` configuration writes are not used.

## Checkpoints

Configured checkpoint interval: 900 s.

Observed checkpoints:

- 2026-06-02 20:12:27 +08:00: `900022` samples, `1000.00049 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 20:27:27 +08:00: `1800015` samples, `999.99303 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 20:42:27 +08:00: `2700057` samples, `999.99257 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 20:57:27 +08:00: `3600051` samples, `999.98951 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 21:12:27 +08:00: `4500043` samples, `999.98901 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 21:27:27 +08:00: `5400037` samples, `999.98896 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 21:42:27 +08:00: `6300030` samples, `999.98902 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 21:57:27 +08:00: `7200023` samples, `999.98906 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 22:12:27 +08:00: `8100017` samples, `999.98917 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 22:27:27 +08:00: `9000010` samples, `999.98922 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 22:42:27 +08:00: `9900003` samples, `999.98910 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 22:57:27 +08:00: `10799996` samples, `999.98907 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 23:12:27 +08:00: `11699990` samples, `999.98923 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 23:27:27 +08:00: `12599983` samples, `999.98933 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 23:42:27 +08:00: `13499976` samples, `999.98936 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-02 23:57:27 +08:00: `14399969` samples, `999.98938 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-03 00:12:27 +08:00: `15299963` samples, `999.98944 Hz`, `0` dropped sync bytes, `0` parse errors.
- 2026-06-03 00:27:27 +08:00: `16199957` samples, `999.98946 Hz`, `0` dropped sync bytes, `0` parse errors.

## Controlled Stop

User requested stopping at about `19 h 15 min` because the partial run was
enough. Codex sent `C-c` to tmux session `kunwei_24h_20260602` at
`2026-06-03 15:12:47 +08:00`, when the process elapsed time was
`19:15:20`.

Final summary written at `2026-06-03T15:12:53`:

- stop reason: `signal_2`
- samples: `69325059`
- duration by first/last sample: `69325.77287676797 s`
- wall elapsed recorded by logger: `69325.79181938001 s`
- rate by first/last sample: `999.9896881529293 Hz`
- dropped sync bytes: `0`
- parse errors: `0`
- raw frames: `1941101652` bytes
- CSV: `18406271618` bytes

## Latency Timer

The manual page mentioning `latency=1` applies to USB serial converters via
`/sys/bus/usb-serial/devices/ttyUSB0/latency_timer`.

This run uses Ethernet TCP. The current Ubuntu machine had no `/dev/ttyUSB*`
device and no USB-serial `latency_timer` path, so there was no applicable
latency timer to set for this capture route.
