# OnRobot HEX-E Hot Start 10 min TCP DAQ Check

- Run directory: `/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/measurements/hot_start_tcpdaq_10min_20260520_172555`
- Started wall time: `2026-05-20T17:25:55`
- Finished wall time: `2026-05-20T17:35:55`
- Protocol: hot start, read-only TCP DAQ from Compute Box `192.168.1.1:49151`
- Safety boundary: READCALIBRATIONINFO once, then READFT only; no bias/filter/speed/zero/TCP/payload/URScript/motion commands.

## Data Products

- Full raw request-response CSV: `onrobot_tcpdaq_hot_start_10min_20260520_172555.csv`
- First 60 s original-row subset: `first_60s_original_rows.csv`
- Raw tuple change CSV: `raw_tuple_changes.csv`
- First 60 s raw tuple change CSV: `first_60s_raw_tuple_changes.csv`
- 10 min plot: `hot_start_10min_raw_tuple_changes_force_torque.png`
- First 60 s plot: `first_60s_raw_tuple_changes_force_torque.png`
- First 60 s force detail: `first_60s_force_detail_fz_fxy.png`
- Analysis JSON: `hot_start_10min_analysis.json`

## Frequency

- Request-response samples: `3533226`
- Request-response duration: `599.997770 s`
- Request-response row rate: `5888.732 Hz`
- Raw tuple change rows: `60306`
- Raw tuple transition rate: `100.510 Hz`
- Mean raw tuple change dt: `9.949 ms`

Interpretation: the TCP loop was polled at about `5888.7 Hz`, but the physical wrench tuple changed at about `100.5 Hz`.

## First 60 s vs Later Drift

Mean change from first 60 s to last 60 s:

| Axis | Delta |
|---|---:|
| `fx_n` | -0.050729 N |
| `fy_n` | -0.031823 N |
| `fz_n` | 0.209377 N |
| `tx_nm` | -0.002951 N m |
| `ty_nm` | -0.005765 N m |
| `tz_nm` | 0.001135 N m |

Mean change from first 5 s to first 60 s:

| Axis | Delta |
|---|---:|
| `fx_n` | -0.004258 N |
| `fy_n` | -0.001849 N |
| `fz_n` | 0.064312 N |
| `tx_nm` | -0.001273 N m |
| `ty_nm` | -0.001281 N m |
| `tz_nm` | 0.000851 N m |


## Visuals

![10 min raw tuple changes](hot_start_10min_raw_tuple_changes_force_torque.png)

![First 60 s raw tuple changes](first_60s_raw_tuple_changes_force_torque.png)

![First 60 s force detail](first_60s_force_detail_fz_fxy.png)

## Offline Filtering Comparison

This section was generated from `raw_tuple_changes.csv`, not the repeated TCP request-response rows. The filtering is offline, zero-phase Butterworth low-pass for analysis and plotting only. No OnRobot `filter_mode`, `speed`, `bias`, or `zero` command was sent.

- Estimated raw-tuple update rate: `100.510 Hz`
- Offline cutoffs tested: `15 Hz`, `5 Hz`, `1.5 Hz`
- Comparison CSV: `offline_filter_comparison_raw_tuple_changes.csv`
- Analysis JSON: `offline_filter_analysis.json`

### First 60 s Fz Smoothness

| Signal | Fz std (N) | Fz peak-to-peak (N) | Step diff RMS (N) | Raw-filter residual std (N) |
|---|---:|---:|---:|---:|
| raw | 0.219594 | 1.520000 | 0.294654 | n/a |
| LP 15 Hz | 0.125467 | 0.918524 | 0.057750 | 0.171077 |
| LP 5 Hz | 0.083456 | 0.701110 | 0.011417 | 0.200408 |
| LP 1.5 Hz | 0.062977 | 0.568319 | 0.002302 | 0.210133 |

### Last 60 s Mean Minus First 60 s Mean

| Signal | Fx (N) | Fy (N) | Fz (N) | Tx (N m) | Ty (N m) | Tz (N m) |
|---|---:|---:|---:|---:|---:|---:|
| raw | -0.050774 | -0.031996 | 0.210725 | -0.002958 | -0.005775 | 0.001141 |
| LP 15 Hz | -0.050792 | -0.031998 | 0.210727 | -0.002957 | -0.005774 | 0.001140 |
| LP 5 Hz | -0.050799 | -0.031982 | 0.210767 | -0.002959 | -0.005775 | 0.001140 |
| LP 1.5 Hz | -0.050817 | -0.031996 | 0.211014 | -0.002958 | -0.005778 | 0.001140 |

Interpretation: offline filtering greatly reduces point-to-point jitter in the first 60 s, especially at `5 Hz` and `1.5 Hz`. It does not remove the slow thermal/static drift: the Fz last-60-minus-first-60 mean remains about `0.21 N` across raw and filtered traces.

![Offline first 60 s filter comparison](offline_filter_first60_fz_fxy.png)

![Offline 10 min Fz filter comparison](offline_filter_10min_fz.png)

![Offline 10 min force axes LP 5 Hz](offline_filter_10min_forces_5hz.png)

