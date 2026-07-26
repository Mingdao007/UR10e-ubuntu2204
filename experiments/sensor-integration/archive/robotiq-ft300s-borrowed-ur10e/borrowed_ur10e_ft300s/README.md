# Borrowed UR10e Robotiq FT 300-S

This folder is for a borrowed/other UR10e equipped with a Robotiq FT 300-S.

Do not merge these measurements into:

- Mingdao main UR10e zero-drift baselines.
- OnRobot HEX-E v2 `3010007655` baselines.
- Any TASE reproduction baseline unless the report explicitly says it is external comparative data.

Current status on 2026-05-19:

- Photo evidence: `photos/IMG_1234.HEIC`, preview `photos/IMG_1234.jpg`.
- Sensor label from onsite/user context: Robotiq `FT 300-S`.
- Mac read check did not discover a reachable UR controller IP.
- UR common ports checked from Mac with no success for `192.168.1.18`, `10.12.0.2`, and `169.254.140.1`.
- Calibration, Zero sensor, firmware, URCap settings, robot motion, payload, and TCP changes are not authorized by this note.

Use `GOAL_RUN_BORROWED_UR10E_FT300S_8H.md` as the copy-ready Ubuntu `/goal` prompt.

