# Move Manifest, OnRobot HEX-E v2 Photo Archive

## Summary

- Source range: `/Users/andyl/Downloads/IMG_1166.HEIC` to `IMG_1205.HEIC`, excluding `IMG_1177.HEIC`.
- Target folder: `/Users/andyl/Documents/UR10e/ft_sensor/onrobot/hex_e_v2_3010007655/photos_raw/`
- File count: 39 HEIC images.
- Move method: copy with metadata preservation, SHA256 verification, then delete original Downloads copies.
- Registry rollback backup: `/Users/andyl/Documents/UR10e/archive/backups/onrobot_hex_e_v2_photo_registry_20260519_000810/`

## Machine-Readable Files

- `move_manifest_20260518.csv`: source, target, size, mtime, and SHA256 recorded before deletion.
- `move_manifest_20260518.sha256`: original source SHA256 output before deletion.

## Notes

- The archive intentionally preserves original camera filenames.
- `IMG_1177.HEIC` was not part of the dropped set and was not present during implementation.
- The registry and report treat this as the first UR10e OnRobot HEX-E v2 loaner-kit photo set.
