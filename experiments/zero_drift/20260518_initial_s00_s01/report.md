# UR10e Zero Drift Initial S00/S01 Report

Recorded: 2026-05-18 late evening HKT

Branch: `exp/ur10e-zero-drift-20260518-initial`

## Purpose

Establish the first UR10e no-motion force baseline after Ethernet bring-up:

- `S00`: quick raw force read before zeroing.
- `S01`: run `zero_ftsensor()`, wait 2 s, then record 700 s of RTDE force.

No robot motion command was sent.

## Robot And Network State

- Robot IP: `192.168.1.18`
- Ubuntu direct link: `enp3s0`, `192.168.1.10/24`
- Dashboard `29999`: open
- Secondary Client `30002`: open
- RTDE `30004`: open
- Remote Control: `true`
- Safety mode: `NORMAL`
- Robot mode: `RUNNING`
- Program running: `false`
- Program state: `STOPPED <unnamed>`
- PolyScope: `URSoftware 5.11.9.1010452 (Jan 24 2022)`

## Payload And TCP Snapshot

Snapshot before the drift run:

- Payload: `1.07 kg`
- Payload CoG: `[0.002, 0.003, 0.057]`
- TCP offset: `[0, 0, 0, 0, 0, 0]`
- Pre-zero force read: approximately `Fx=-11.42 N`, `Fy=5.14 N`, `Fz=-25.01 N`, `|F|=27.97 N`

## Commands

S00 quick raw sample:

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/sample_tcp_force.py \
  --seconds 5 --hz 20 --plot both \
  --prefix S00_quick_nozero_5s
```

S01 zero drift:

```bash
python3 /home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/measure_zero_drift_after_zeroft.py \
  --confirm-no-contact \
  --seconds 700 --hz 20 \
  --prefix S01_rezero_700s
```

## Artifacts

Data:

- `data/S00_quick_nozero_5s_20260518_234115.csv`
- `data/S01_rezero_700s_20260518_235354.csv`

Plots:

- `plots/S00_quick_nozero_5s_20260518_234115.png`
- `plots/S00_quick_nozero_5s_20260518_234115_fz.png`
- `plots/S01_rezero_700s_fz_20260518_235354.png`

## Results

S00 quick raw sample, 5.05 s, 101 samples:

| Axis | Mean | Std | Min | P50 | Max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fx N | -11.344 | 0.249 | -11.988 | -11.345 | -10.844 |
| Fy N | 4.850 | 0.296 | 4.273 | 4.868 | 5.623 |
| Fz N | -25.056 | 0.256 | -25.678 | -25.045 | -24.276 |
| \|F\| N | 27.931 | 0.255 | 27.197 | 27.915 | 28.631 |

S01 after `zero_ftsensor()`, 700.03 s, 14001 samples:

| Axis | Mean | Std | Min | P1 | P50 | P99 | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fx N | -1.742 | 0.854 | -3.792 | -3.281 | -1.785 | -0.116 | 0.281 |
| Fy N | 0.309 | 0.409 | -1.017 | -0.603 | 0.302 | 1.240 | 1.651 |
| Fz N | -3.560 | 1.940 | -7.522 | -6.916 | -3.729 | 0.030 | 0.628 |
| \|F\| N | 4.018 | 2.078 | 0.058 | 0.404 | 4.167 | 7.587 | 8.361 |

S01 Fz drift:

- Start 5 s mean: `-0.182 N`
- End 5 s mean: `-6.556 N`
- End-start delta: `-6.375 N`
- Linear slope: `-0.009469 N/s`
- `|Fz| > 1 N`: `12316` samples
- `|Fz| > 2 N`: `10217` samples
- `|Fz| > 5 N`: `4033` samples

## Conclusion

The UR10e is connected and readable, but this first after-zero 700 s run is not
a stable zero-drift baseline. Fz drifted negative by about `6.4 N` across the
window, with many samples exceeding `|Fz| > 5 N`.

Do not proceed to force-control or contact experiments from this evidence.

## Next Step

Before the next run, physically inspect whether the tool, cable, wrist, or any
fixture is slowly loading the end effector. Then repeat a 700 s after-zero run
under the same no-motion conditions. If the same trend repeats, run the thermal
drift script with `tool_temperature` and re-check payload/TCP/tool mounting.
