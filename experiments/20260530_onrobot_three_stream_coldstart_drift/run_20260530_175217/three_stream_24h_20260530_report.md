# UR10e / OnRobot Three-Stream Cold-Start Drift Report

Generated: `2026-05-31T02:47+08:00`

## Executive Summary

This run captured enough no-motion data for the current drift comparison task.
The logger was stopped intentionally after the user confirmed that the dataset
was sufficient. The capture script exited cleanly with `ok: true`, `errors: []`,
and `stop_reason: signal_2`.

The dataset contains about `8 h 47 min 12 s` of simultaneous three-stream data:

| Stream | Samples | Effective rate | Notes |
|---|---:|---:|---|
| UR RTDE `actual_TCP_force` + speed + URCap registers | `15,816,006` rows | `499.998 Hz` RTDE table rate | UR built-in force tuple changed every RTDE row. |
| OnRobot URCap variables via `output_double_register_24..29` | `3,936,874` tuple transitions | `124.458 Hz` over full run | Mostly 4 RTDE rows/update; full-run rate is pulled down by the known `3.urp` stop/stale interval. |
| OnRobot high-speed UDP raw | `15,803,493` packets | `499.603 Hz` | Sequence delta `+1` throughout; sample-counter delta `+2` throughout. |

The useful conclusion from this run is that the two OnRobot streams remained
stable enough for long drift analysis after recovery, and the UDP stream was
continuous and clean over the full captured period. The UR built-in
`actual_TCP_force` channel drifted far more than the OnRobot channels in this
static setup, especially in Fz.

## Files

Run directory:

`/home/andy/ur10e_ros2_ws/experiments/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217`

Primary artifacts:

| Artifact | Path |
|---|---|
| Summary JSON | `three_stream_24h_20260530_20260530_175220_summary.json` |
| Checkpoint JSON | `three_stream_24h_20260530_20260530_175220_checkpoint.json` |
| RTDE / URCap CSV | `three_stream_24h_20260530_20260530_175220_rtde_ur500_urcap125.csv` |
| OnRobot UDP raw CSV | `three_stream_24h_20260530_20260530_175220_onrobot_udp500_raw.csv` |
| Fz overlay plot | `plots/three_stream_24h_20260530_20260530_175220_first_zero_fz_overlay.png` |
| Force-axis overlay plot | `plots/three_stream_24h_20260530_20260530_175220_first_zero_force_axes.png` |
| Operator log | `RUN_STATUS.md` |

Final on-disk size was about `6.4G`.

## Capture Boundary

Safety and command boundary preserved during the run:

- No robot motion commands.
- No URScript upload/run from Ubuntu.
- No UR `zero_ftsensor()`.
- No UR TCP/payload writes.
- No OnRobot `BIAS` or `FILTER`.
- OnRobot UDP commands sent by the logger were only `SPEED=2`, `START`, and final `STOP`.
- First-sample subtraction is analysis zero only; it is not a device zero.

UDP command record:

| Command | Wall time | Data |
|---|---|---:|
| `SPEED` | `2026-05-30T17:52:22.331` | `2` |
| `START` | `2026-05-30T17:52:22.336` | `51840000` |
| `STOP` | `2026-05-31T02:39:34.446` | `0` |

Dashboard before and after the logger both reported:

- `Robotmode: RUNNING`
- `Safetymode: NORMAL`
- `Program running: true`
- `programState: PLAYING 3.urp`
- Remote control: `false`

The logger is stopped. The PolyScope program was still playing at the final
readback; stop it manually on the pendant if it is no longer needed.

## Data Quality

The final summary reported `errors: []`.

RTDE timing:

- Samples: `15,816,006`
- First-last duration: `31,632.107541431 s`
- Interval rate: `499.998458 Hz`
- Mean dt: `2.000006 ms`
- p99 dt: `2.060446 ms`
- Max dt: `17.847828 ms`

OnRobot UDP timing:

- Samples: `15,803,493`
- First-last duration: `31,632.103973266 s`
- Interval rate: `499.602935 Hz`
- Mean dt: `2.001590 ms`
- p99 dt: `2.109547 ms`
- Max dt: `16.287933 ms`

OnRobot UDP packet integrity:

- Status counts: `0 -> 15,803,493`
- Sequence delta counts: `+1 -> 15,803,492`
- Sample-counter delta modulo 65536: `+2 -> 15,803,492`

URCap register update behavior:

- Tuple transitions: `3,936,874`
- Full-run transition rate: `124.458131 Hz`
- Mean rows/update: `4.0174`
- Median rows/update: `4`
- Dominant run length: `4` RTDE rows/update, `3,936,834` occurrences

The `max_rows_per_run: 68448` is not normal steady-state URCap behavior. It is
the known stale interval caused by the temporary `3.urp` stop after the program
rename event.

## Known `3.urp` Stop / Stale Interval

At `2026-05-30T18:19:01+08:00`, Dashboard showed:

- `Program running: false`
- `programState: STOPPED 3.urp`

The capture process and CSV writes continued, but the URCap register stream
became stale because the no-motion export program was not running. The user
later explained this was caused by renaming `3.urp`, then restarted it from the
pendant.

Manual evidence in `RUN_STATUS.md` found the stale URCap tuple run:

- Start sample: `791305`
- Start `t_s`: `1582.621273352`
- End sample: `859752`
- End `t_s`: `1719.516048058`
- Duration: `136.894774706 s`

Analysis note: exclude this interval for URCap-register drift/update-rate
analysis. The UR RTDE table and OnRobot UDP packet stream continued, but the
URCap register values in this interval are not valid live URCap values.

## First-Sample-Zero Drift Snapshot

The following values are from `zeroed_axis_stats` in the final summary. Units
are N for force and Nm for torque.

| Channel / axis | Last - first | Zeroed mean | Zeroed std | Zeroed min | Zeroed max |
|---|---:|---:|---:|---:|---:|
| UR `actual_TCP_force` Fx | `-3.2669` | `-3.2081` | `1.4007` | `-5.6784` | `1.8748` |
| UR `actual_TCP_force` Fy | `-9.9775` | `-11.9801` | `2.9451` | `-16.3522` | `0.8827` |
| UR `actual_TCP_force` Fz | `-70.5431` | `-48.2999` | `18.5753` | `-71.4719` | `2.2148` |
| OnRobot URCap Fx | `0.0117` | `-0.0109` | `0.0949` | `-0.4183` | `0.4900` |
| OnRobot URCap Fy | `0.0247` | `-0.0047` | `0.0627` | `-0.2753` | `2.9938` |
| OnRobot URCap Fz | `0.2546` | `-0.2165` | `0.4329` | `-24.4809` | `1.5746` |
| OnRobot UDP Fx | `0.0000` | `0.0866` | `0.0824` | `-0.3200` | `0.4900` |
| OnRobot UDP Fy | `0.2300` | `0.2660` | `0.0603` | `-0.1500` | `0.5500` |
| OnRobot UDP Fz | `0.6300` | `0.2016` | `0.4434` | `-1.5200` | `2.1500` |

Interpretation:

- The UR built-in force channel shows large long-run apparent drift in this
  no-motion setup, especially Fz.
- The OnRobot UDP raw channel stays within a few N first-sample-zeroed range
  over the full run.
- The OnRobot URCap Fz table includes an outlier minimum from the known stale /
  restart interval. Recompute URCap drift after excluding the stale interval for
  a cleaner estimate.
- Do not interpret absolute values across UR built-in, URCap, and UDP as
  equivalent zero/reference frames. Use first-sample-zeroed trends unless a
  separate same-time zero/reference equivalence test is performed.

## Recommended Analysis Use

Use this run for:

- Long-duration OnRobot UDP packet continuity at `SPEED=2`.
- Long-duration UR RTDE timing stability.
- Comparing first-sample-zeroed trends across UR built-in force, OnRobot URCap
  register export, and OnRobot UDP raw stream.
- Estimating practical drift behavior after excluding the known URCap stale
  interval.

Do not use this run for:

- Absolute force-value equivalence between UR built-in force, OnRobot URCap
  variables, and OnRobot UDP raw values.
- Claims that the URCap register stream had no interruption; it had one known
  stale interval from the stopped `3.urp`.
- Contact-force or force-control conclusions. This was a no-motion logging run.

Minimum exclusion for URCap-register analysis:

```text
exclude t_s in [1582.621273352, 1719.516048058]
```

Optional stricter exclusion:

```text
exclude a small guard band around that interval, e.g.
[1575, 1730] seconds from logger start
```

## Bottom Line

The run is successful as a long no-motion three-stream capture. The final
logger summary is clean, the UDP stream is packet-continuous, and the final
`STOP` was sent. The only known data-quality caveat is the manually documented
`3.urp` stop/stale interval, which should be excluded from URCap-register
analysis.
