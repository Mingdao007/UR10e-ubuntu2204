# Kunwei KWR75B 1 kHz 24h Capture - First 15 Minute Report

Report generated: `2026-06-02`

## Summary

The first checkpoint confirms that the Kunwei KWR75B TCP capture entered a
stable 1 kHz receive state for the first 15 minutes.

- Capture start: `2026-06-02T19:57:27+08:00`
- First checkpoint time: `2026-06-02T20:12:27+08:00`
- Transport: TCP client
- Connected peer: `192.168.50.25:5152`
- Samples in checkpoint: `900,022`
- Time span from first to last sample: `900.020558 s`
- Estimated sample rate: `1000.000491 Hz`
- Parse errors: `0`
- Dropped sync bytes: `0`
- Received command byte: `0x48` for all `900,022` frames

This is a no-motion, no-contact interface and stability check. It does not
prove force calibration accuracy, robot mounting correctness, or contact-force
performance.

## Run Artifacts

Run directory:

`/home/andy/ur10e_ros2_ws/ft_sensor/kunwei/kwr75b/measurements/19h15min/capture`

Key files:

- `metadata.json`
- `checkpoint.json`
- `data.csv`
- `raw_frames.bin`

The live process was still running when this report was written. The checkpoint
summary's `stop_reason: "duration"` describes the checkpoint window, not the
24-hour process ending.

## Capture Configuration

Command route:

- `capture_kunwei_kwr75_1khz.py`
- `--transport tcp-client`
- `--sensor-ip 192.168.50.25`
- `--sensor-port 5152`
- `--duration-s 86400`
- `--checkpoint-interval-s 900`
- `--connect-timeout-s 5`

Manual basis recorded in metadata:

- Stream command: `48 AA 0D 0A`
- Stop command: `43 AA 0D 0A`
- Frame size: `28` bytes
- Manual stream rate: `1 kHz`
- Raw force units preserved as manual `Kg`
- Raw moment units preserved as manual `Kg*m`
- Configuration write prefix `EE AA` was not sent
- UDP configuration ports `5152/5153` were not used for configuration writes

## Timing And Transport Quality

| Metric | Value |
| --- | ---: |
| Samples | `900,022` |
| First-last duration | `900.020558 s` |
| Wall elapsed | `900.038730 s` |
| Rate by first-last duration | `1000.000491 Hz` |
| Mean dt | `0.9999995 ms` |
| Min dt | `0.006061 ms` |
| Max dt | `50.609597 ms` |
| Packets received | `49,896` |
| Bytes received | `25,200,636` |
| Parse errors | `0` |
| Dropped sync bytes | `0` |

Interpretation:

- The average timing matches the manual 1 kHz rate.
- `0` parse errors and `0` dropped sync bytes mean the frame parser stayed
  synchronized for the whole first checkpoint window.
- The `50.6 ms` max inter-sample gap is a transport/timestamping warning to
  track in later checkpoints. It does not by itself indicate missing parsed
  frames because the total first-window sample count and average rate are
  correct.

## Axis Statistics

Raw values below preserve the manual units from the KWR75 documentation.

| Axis | First | Last | Last - First | Mean | Std | Min | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `Fx_kg_manual` | `0.248601` | `0.248275` | `-0.000326` | `0.248025` | `0.001382` | `0.237254` | `0.257905` |
| `Fy_kg_manual` | `-0.111381` | `-0.113185` | `-0.001803` | `-0.112258` | `0.001269` | `-0.124002` | `-0.104666` |
| `Fz_kg_manual` | `-0.094687` | `-0.093809` | `0.000878` | `-0.093931` | `0.001212` | `-0.163522` | `-0.050684` |
| `Mx_kg_m_manual` | `0.005307` | `0.005270` | `-0.0000367` | `0.005282` | `0.0000339` | `0.004761` | `0.005660` |
| `My_kg_m_manual` | `-0.006510` | `-0.006494` | `0.0000154` | `-0.006501` | `0.0000339` | `-0.006885` | `-0.006141` |
| `Mz_kg_m_manual` | `0.006199` | `0.006200` | `0.00000128` | `0.006217` | `0.0000342` | `0.006051` | `0.006425` |

Derived SI values below assume manual `Kg` means kilogram-force and multiply by
`9.80665`. Treat these as derived convenience values, not raw sensor output.

| Axis | Mean SI | Std SI | Drift SI |
| --- | ---: | ---: | ---: |
| `Fx` | `2.432290 N` | `0.013556 N` | `-0.003199 N` |
| `Fy` | `-1.100876 N` | `0.012446 N` | `-0.017685 N` |
| `Fz` | `-0.921148 N` | `0.011884 N` | `0.008606 N` |
| `Mx` | `0.051802 N*m` | `0.000332 N*m` | `-0.000360 N*m` |
| `My` | `-0.063751 N*m` | `0.000332 N*m` | `0.000151 N*m` |
| `Mz` | `0.060973 N*m` | `0.000335 N*m` | `0.000013 N*m` |

## First 15 Minute Judgment

Pass for the intended first checkpoint gate:

- The capture remained connected through the first 15 minutes.
- The sample count and first-last duration match 1 kHz operation.
- The parser saw only `0x48` streaming frames and did not lose sync.
- No configuration write command was sent during this capture.
- The measured force/moment values stayed small and stable enough for a
  no-motion warm-up baseline.

Items to keep watching in later checkpoints:

- Whether `max_dt_ms` remains an occasional timestamp artifact or repeats as a
  sustained transport gap.
- Whether force means drift materially over hours after warm-up.
- Whether the raw CSV size stays within available disk budget for the full
  24-hour run.
- Whether the status LEDs remain visually normal while live frames continue.

## Next Checks

- Recheck the checkpoint file after `30 min`, `1 h`, and later multi-hour
  windows.
- After the 24-hour run finishes, produce a full drift/noise report from the
  complete CSV and raw frame log.
- Keep raw manual-unit columns as the source of truth; add SI columns only as
  derived analysis fields.
