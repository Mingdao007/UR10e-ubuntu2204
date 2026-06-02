# Kunwei KWR75B Current State

Last updated: 2026-06-02

## Source Evidence

- Mac root: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei`
- Initial photo set: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260602_kwr75b_initial_drop`
- Raw photos: `IMG_1621.HEIC` through `IMG_1659.HEIC`, 39 total
- Contact sheet: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260602_kwr75b_initial_drop/assets/contact_sheet_20260602_kwr75b_initial_drop.jpg`
- Document manifest: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei/docs_manifest.yml`

## Manuals

- TCP manual: KWR75 RS422 to Ethernet-TCP, 28-byte frames, 460800 serial baud.
- UDP manual: KWR75 RS422 to Ethernet-UDP, 28-byte frames, 460800 serial baud.

## Current Interpretation

- Frame format: `0x48/0x49`, `0xAA`, six little-endian float components,
  `0x0D 0x0A`.
- Manual component order: `Fx`, `Fy`, `Fz`, `Mx`, `My`, `Mz`.
- Manual raw units: `Kg` and `Kg*m`; SI conversion should be explicit in
  reports.
- Serial-server default network facts from manuals: IP `192.168.50.25`,
  gateway `192.168.50.1`, target IP `192.168.50.26`, target port `8886`,
  config receive port UDP `5152`, reply port UDP `5153`.

## Next Work

- Confirm exact model/serial from close-up photo or label.
- Intake the installation recording when available.
- Do only no-motion, no-contact interface bring-up before any robot-side
  integration.

