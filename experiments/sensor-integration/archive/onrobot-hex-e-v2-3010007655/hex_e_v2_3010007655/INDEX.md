# OnRobot HEX-E v2 3010007655 Index

## Current Entry Points

- Device root readme: [README.md](README.md)
- Latest prep report: [docs/reports/onrobot_hex_e_v2_reproduction_prep_20260518.md](docs/reports/onrobot_hex_e_v2_reproduction_prep_20260518.md)

## Goals

- No current Socket.IO long-run goals. The old 6 h / 8 h Socket.IO goals are archived.

## Measurements

- `measurements/hot_start_tcpdaq_10min_20260520_172555`
- `measurements/udp_highspeed_safe_probe_20260526_202734`
- `measurements/udp_highspeed_open_reference_20260526_204128`
- `measurements/udp_highspeed_open_reference_20260526_204143`
- `measurements/urcap_variables_rtde_register_baseline_20260526`
- `measurements/urcap_variables_rtde_register_mapped_20260526`
- `measurements/urcap_variables_rtde_register_mapped_600s_20260526`
- `measurements/urcap_variables_rtde_register_same_screen_20260526`
- `measurements/urcap_variables_rtde_register_snapshot_check_20260526`
- `measurements/demo_1_1_data_path_check_20260527`
- `measurements/demo_1_1_script10_contact_20260527`
- `measurements/demo_1_1_script10_5n_contact_20260528`
- `measurements/demo_1_1_script10_5n_10mms_contact_20260528`
- `measurements/demo_1_2_path_straight_line_urcap_20260528_012054`
- `measurements/demo_1_2_onrobot_fz_urcap_20260528_013344`
- `measurements/demo_1_2_auto_path_20260528_015805`
- `measurements/demo_1_2_final_success_20260528_022654`

Measurement run directories may contain large raw CSV files and generated
plots. Keep them in place; track only small reports, summaries, metadata, and
scripts when needed.

## Manifests

- Intake: [manifests/20260518_intake/](manifests/20260518_intake/)
- Orientation: [manifests/20260519_orientation/](manifests/20260519_orientation/)
- Transfer: [manifests/20260519_transfer/](manifests/20260519_transfer/)

## Archives

- Reproduction prep drafts: [docs/archive/20260518_reproduction_prep/](docs/archive/20260518_reproduction_prep/)
- Embedded skill copy: [docs/archive/embedded_skills/md-report-skill/](docs/archive/embedded_skills/md-report-skill/)
- Socket.IO 9 Hz runbooks: [docs/archive/socketio_9hz_20260519_20260520/](docs/archive/socketio_9hz_20260519_20260520/)
- Socket.IO 9 Hz measurements: [measurements/archive/socketio_9hz_20260519_20260520/](measurements/archive/socketio_9hz_20260519_20260520/)

## Tools

- [tools/analyze_onrobot_drift.py](tools/analyze_onrobot_drift.py)
- [tools/onrobot_socketio_logger.py](tools/onrobot_socketio_logger.py) - legacy Socket.IO 9 Hz logger; archived path only
- [tools/onrobot_udp_safe_probe.py](tools/onrobot_udp_safe_probe.py)
- [tools/onrobot_udp_open_reference.py](tools/onrobot_udp_open_reference.py)
- [docs/runbooks/README_UDP_HIGHSPEED_PROBES.md](docs/runbooks/README_UDP_HIGHSPEED_PROBES.md)

## Vault References

- Canonical vault root: `/home/andy/ur10e_lab_vault/onrobot/hex_e_v2_3010007655`
- SOPs: `/home/andy/ur10e_lab_vault/onrobot/hex_e_v2_3010007655/sop/`
