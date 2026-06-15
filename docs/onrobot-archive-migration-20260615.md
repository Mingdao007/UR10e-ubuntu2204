# OnRobot Archive Migration - 2026-06-15

This migration moves retired OnRobot evidence out of active workspace
navigation and into archive zones. It preserves original basenames and does not
rename raw files.

## Policy

- OnRobot is historical evidence.
- Kunwei, zero_ftsensor, UR10e live code, and current Step5/TP work are out of
  scope.
- Mixed-support experiment directories remain in place when moving them would
  damage TASE or demo context.
- Raw CSV/JSON/log payload contents are not rewritten. Navigation files,
  scripts, reports, and exact references are updated.

## Directory Mapping

| Old path | New path |
|---|---|
| `experiments/20260520_ur10e_poweron_readonly` | `experiments/archive/onrobot/20260520_ur10e_poweron_readonly` |
| `experiments/20260523_onrobot_tcpdaq_post_urcap_10min` | `experiments/archive/onrobot/20260523_onrobot_tcpdaq_post_urcap_10min` |
| `experiments/20260523_payload_tcp_static_validation` | `experiments/archive/onrobot/20260523_payload_tcp_static_validation` |
| `experiments/20260523_ur10e_onrobot_switch_readonly_60s` | `experiments/archive/onrobot/20260523_ur10e_onrobot_switch_readonly_60s` |
| `experiments/20260524_onrobot_tcp_vs_polyscope_variables` | `experiments/archive/onrobot/20260524_onrobot_tcp_vs_polyscope_variables` |
| `experiments/20260528_dual_ur500_onrobot_registers_probe` | `experiments/archive/onrobot/20260528_dual_ur500_onrobot_registers_probe` |
| `experiments/20260528_dual_ur_onrobot_rtde_probe` | `experiments/archive/onrobot/20260528_dual_ur_onrobot_rtde_probe` |
| `experiments/20260528_onrobot_three_stream_600s_first_zero` | `experiments/archive/onrobot/20260528_onrobot_three_stream_600s_first_zero` |
| `experiments/20260528_onrobot_three_stream_coldstart_drift` | `experiments/archive/onrobot/20260528_onrobot_three_stream_coldstart_drift` |
| `experiments/20260528_static_three_stream_capture` | `experiments/archive/onrobot/20260528_static_three_stream_capture` |
| `experiments/20260530_onrobot_three_stream_coldstart_drift` | `experiments/archive/onrobot/20260530_onrobot_three_stream_coldstart_drift` |
| `experiments/onrobot_hex_e_v2_3010007655` | `experiments/archive/onrobot/onrobot_hex_e_v2_3010007655` |
| `ft_sensor/onrobot` | `experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655` |
| `report/onrobot_three_stream_90min_20260528.md` | `report/archive/onrobot/onrobot_three_stream_90min_20260528.md` |
| `report/onrobot_three_stream_coldstart_drift_20260528.md` | `report/archive/onrobot/onrobot_three_stream_coldstart_drift_20260528.md` |
| `report/onrobot_three_stream_half_hour_20260528.md` | `report/archive/onrobot/onrobot_three_stream_half_hour_20260528.md` |
| `report/onrobot_three_stream_static_capture_20260528.md` | `report/archive/onrobot/onrobot_three_stream_static_capture_20260528.md` |
| `report/assets/onrobot_three_stream_90min_20260528` | `report/assets/archive/onrobot/onrobot_three_stream_90min_20260528` |
| `report/assets/onrobot_three_stream_coldstart_drift_20260528` | `report/assets/archive/onrobot/onrobot_three_stream_coldstart_drift_20260528` |
| `report/assets/onrobot_three_stream_half_hour_20260528` | `report/assets/archive/onrobot/onrobot_three_stream_half_hour_20260528` |
| `report/assets/onrobot_three_stream_static_capture_20260528` | `report/assets/archive/onrobot/onrobot_three_stream_static_capture_20260528` |

## Mixed-Support Directories Kept Active

| Path | Reason |
|---|---|
| `experiments/20260525_tase_sim_readable_state_backup` | TASE reproduction context with OnRobot-era evidence. |
| `experiments/20260527_ur10e_demo_1_1_local_planar_patch` | Demo context with OnRobot-era validation evidence. |
