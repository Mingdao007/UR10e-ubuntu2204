# UR10e Kunwei KWR75B Trial Unit

This is the Ubuntu working root for Kunwei/KWR75B analysis on the current
UR10e bench.

Mac source of truth:

`/Users/andyl/Documents/UR10e/ft_sensor/kunwei`

Current first photo set:

`/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260602_kwr75b_initial_drop`

Use this root for generated logs, parsed CSVs, reports, and tools that operate
on copied or live-captured data. Do not copy raw Mac originals here unless a
task explicitly needs local image/video processing.

Current deterministic parser:

`/home/andy/codex-private-skills/skills/ur10e-kunwei-kwr75/scripts/parse_kwr75_frame.py`

Directory intent:

- `tools/`: Kunwei-specific helpers or wrappers for this workspace.
- `measurements/`: dated live capture outputs, raw bytes, parsed CSVs, metadata.
- `reports/`: analysis reports and comparison outputs.

