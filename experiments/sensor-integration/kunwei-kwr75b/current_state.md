# Kunwei KWR75B Current State

Last updated: 2026-08-21

## Current EOAT and Calibration Handoff

The canonical image crosswalk and full calibration procedure are maintained in
the private Kunwei skill reference:

`/home/andy/codex-private-skills-shared-main/skills/ur10e-kunwei-kwr75/references/current-eoat-photo-and-calibration.md`

Current live UR read-back:

- payload: `1.33 kg`
- CoG: `[-2,-6,65] mm`
- TCP offset: `[0,0,262.6,0,0,0] mm` (unchanged)
- final check: `Safety NORMAL`, `STOPPED 1.urp`, TCP speed zero

Current Mac photo set:

`/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260821_rg2_payload_calibration`

Use `IMG_2284.JPG` (`891.0 g`, wired RG2/QC) as the current physical assembly
mass reference. `IMG_2285.JPG` (`873.0 g`, unwired) is reference-only. The old
white adapter in `IMG_1688` is excluded from the current experiment.

The independent Kunwei fit (`1.0617 kg`, Kunwei sensing-plane frame) is
diagnostic and must not replace the UR payload value. The next controller task
is SFC reproduction; Autotuner is only the existing UR10e bench environment.

## Source Evidence

- Mac root: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei`
- Initial photo set: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260602_kwr75b_initial_drop`
- Mounted/weight/TCP photo set: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260604_kwr75b_mounted_weight_tcp`
- Wiring-route video set: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei/wiring_videos/20260604_ur10e_kunwei_cable_route`
- Raw photos: `IMG_1621.HEIC` through `IMG_1659.HEIC`, 39 total
- Contact sheet: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260602_kwr75b_initial_drop/assets/contact_sheet_20260602_kwr75b_initial_drop.jpg`
- Current mounted contact sheet: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei/photo_sets/20260604_kwr75b_mounted_weight_tcp/assets/contact_sheet_20260604_kwr75b_mounted_weight_tcp.jpg`
- Current wiring contact sheet: `/Users/andyl/Documents/UR10e/ft_sensor/kunwei/wiring_videos/20260604_ur10e_kunwei_cable_route/assets/contact_sheet_20260604_wiring_route.jpg`
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
- TCP manual Figure 3 defines the sensor coordinate frame: in the top view
  `Fx` points right and `Fy` points up; in the side view `Fz` points up. The
  figure caption states that moment directions follow the right-hand rule.
- Gravity-axis calibration on 2026-06-11 with wrist3 four-quadrant sweep
  supports the XY mapping `F_T_x = +Fx_K`, `F_T_y = +Fy_K`. The previous bridge
  mapping `F_T_y = -Fy_K` is inconsistent with that run.
- Current bridge mapping after the 2026-06-11 right-hand correction is
  `F_T=[Fx_K,Fy_K,Fz_K]`, `M_T=[Mx_K,My_K,Mz_K]`. The Z sign follows the
  right-handed-frame constraint given the XY result, but it still needs direct
  Z-excitation validation before contact normal-load sign decisions.
- Pre-2026-06-11 runs that used Kunwei `Fx/Fy` for attitude, contact offset, or
  lateral interpretation should be treated as mapping-uncalibrated evidence.
  Fz-only force-control evidence is not automatically invalidated by the XY
  correction, but still depends on the unresolved Z sign.
- Serial-server default network facts from manuals: IP `192.168.50.25`,
  gateway `192.168.50.1`, target IP `192.168.50.26`, target port `8886`,
  config receive port UDP `5152`, reply port UDP `5153`.
- Historical mass bookkeeping: `0.331 kg` for the Kunwei sensor-side stack
  shown in `IMG_1691`; the old white adapter/tool is excluded from the current
  experiment and must not be added.
- Current TCP-length candidates: `122.34 mm` from the UR/native tool and about
  `126.1 mm` from caliper measurement. This is not resolved.
- Default remote-control architecture: Python Kunwei TCP logger plus Python
  UR Dashboard/RTDE read-only supervision first; ROS 2 external F/T integration
  later if needed; MATLAB only for MATLAB-specific tasks.

## Next Work

- Run a separate Z-excitation gravity pose before treating contact normal-load
  sign as calibrated.
- Confirm exact serial from close-up photo or label only when serial-specific
  traceability is needed; ordinary current-unit work uses the `kwr75b` root.
- Run a short no-motion regression capture after the current cable routing is
  settled.
- Payload/CoG was validated through the PolyScope wizard and fresh
  `ur10e-realsetup` read-back on 2026-08-21. TCP remains unresolved between the
  `122.34 mm` native and about `126.1 mm` caliper candidates.
- Keep Windows SensorLinker as vendor reference; use Ubuntu/Python as the
  primary collector.
