# OnRobot HEX-E v2 3010007655 Device Root

This is the canonical Ubuntu device root for the OnRobot HEX-E v2 sensor with
stable identity `hex_e_v2_3010007655`.

Do not add intake dates to this root directory. Put dates on measurements,
reports, manifests, and photo batches.

## Directory Roles

- `INDEX.md`: current map of useful reports, runs, manifests, and tools.
- `docs/`: human-written goals, handoffs, runbooks, reports, and archived drafts.
- `manifests/`: transfer, intake, orientation, checksum, and provenance records.
- `measurements/`: raw and derived measurement runs. Keep run directories intact.
- `photos_raw/`: original phone/camera photos. Do not track in git.
- `latest_connection_photos/`: temporary/latest connection photo views. Do not track in git.
- `assets/`: generated visual assets, thumbnails, and contact sheets.
- `tools/`: local analysis and logging helpers for this device root.

## Safety Boundary

This directory is for offline organization, reports, and no-motion OnRobot data
work. Organizing files here must not imply approval to run URCap installs,
OnRobot zero/bias/filter/speed/TCP writes, URScript, force control, or robot
motion.

Use the UR10e/OnRobot SOPs before any hardware action. Current user-facing SOPs
live under:

```text
/home/andy/ur10e_lab_vault/onrobot/hex_e_v2_3010007655/sop/
```

## Git/Data Policy

Track small control and provenance files when useful:

- Markdown reports and runbooks
- Python tools
- YAML/config files
- manifest, summary, metadata, and checksum files

Do not track raw or photo-like data:

- `photos_raw/`
- `latest_connection_photos/`
- `assets/thumbnails/`
- `assets/contact_sheets/`
- `measurements/**/*.csv`
- `measurements/**/*.jsonl`
- `measurements/**/*.png`
- `measurements/**/*.log`

For files that are otherwise eligible, use `5 MiB` as the practical upper bound
before deciding whether they belong in git.

