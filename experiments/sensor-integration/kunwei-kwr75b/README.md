# UR10e Kunwei KWR75B

This is the Ubuntu working root for Kunwei/KWR75B analysis on the current
UR10e bench.

Mac source of truth:

`/Users/andyl/Documents/UR10e/ft_sensor/kunwei`

Current first photo set:

`/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260602_kwr75b_initial_drop`

Current mounted/weight/TCP photo set:

`/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260604_kwr75b_mounted_weight_tcp`

Current wiring-route video set:

`/Users/andyl/Documents/UR10e/ft_sensor/kunwei/wiring_videos/20260604_ur10e_kunwei_cable_route`

Use this root for generated logs, parsed CSVs, reports, and tools that operate
on copied or live-captured data. Do not copy raw Mac originals here unless a
task explicitly needs local image/video processing.

Current deterministic parser:

`/home/andy/codex-private-skills/skills/ur10e-kunwei-kwr75/scripts/parse_kwr75_frame.py`

Directory intent:

- `tools/`: Kunwei-specific helpers or wrappers for this workspace.
- `measurements/`: live capture outputs, raw bytes, parsed CSVs, metadata.
  Measurement folders use either feature or date/time, not both. Prefer
  feature-only names such as `19h15min` when the report/metadata already
  records the exact date and route.
- `reports/`: analysis reports and comparison outputs.
- `evidence/`: local notes that summarize Mac-filed photos/videos without
  copying raw Mac originals into Ubuntu.
