# URCap Discovery Preflight

Date: 2026-05-23

Scope: read-only preflight for OnRobot URCap discovery on the current UR10e /
HEX-E v2 / Compute Box bench. No template program, zero, bias, filter, speed,
TCP, payload, URScript, or robot motion was commanded.

## What Is Already Proven

- Ubuntu Ethernet `enp3s0` is connected as `192.168.1.10/24`.
- UR10e `192.168.1.18` is reachable by ping with no packet loss.
- UR10e Dashboard is reachable and reports:
  - `Safetymode: NORMAL`
  - `Robotmode: RUNNING`
  - `Program running: false`
  - `programState: STOPPED <unnamed>`
  - `URSoftware 5.11.9.1010452`
- UR10e ports `22`, `29999`, `30001`, `30002`, `30003`, `30004`, `30011`,
  `30012`, and `30013` are open.
- OnRobot Compute Box `192.168.1.1` is reachable on:
  - HTTP `80`
  - CBRCI realtime `32000`
  - CBRCI command `32002`
  - TCP DAQ `49151`
- Compute Box HTTP `/version` returns `4.1.8`.
- Compute Box high-speed UDP port `49152` is not a TCP service; TCP connect to
  `49152` is refused, which does not test UDP availability.
- Follow-up PolyScope photos `IMG_1358.HEIC` through `IMG_1365.HEIC`, filed on
  Mac under
  `/Users/andyl/Documents/UR10e/ft_sensor/incoming_to_classify/20260523_urcap_discovery_onrobot_ft_setup/`,
  add the following evidence:
  - PolyScope `OnRobot FT Setup - HEX` selects `192.168.1.1`.
  - Status is `OK`.
  - Sensor system health is `OK`.
  - The page reports Compute Box version `4.1.8`.
  - The page reports UR robot IP `192.168.1.18` and subnet mask
    `255.255.255.0`.
  - The captured serial number reads `HEX-EB806`.
  - The device is marked as mounted on the OnRobot Quick Changer.
  - `Use UR default TCP Configuration` is selected; `Set from sensor flange`
    is not selected on the captured TCP page.
  - The PolyScope URCaps page shows `FT-OnRobot` version `4.1.7` by OnRobot
    A/S installed.
  - The unrelated `External Control` page shows host IP `192.168.56.1` and
    custom port `50002`.

## What The Existing Photos And Preflight Do Not Prove

- They do not prove URCap feedback variables are updating at the documented
  `~125 Hz` on this bench.
- They do not prove a no-motion URCap variable capture can run without a
  PolyScope program or URCap-generated script.
- They do not identify a URCap force variable name, output register, or RTDE
  register mapping that Ubuntu can log directly.

## Blocked Ubuntu-Side Check

Read-only SSH inspection of the UR controller filesystem was attempted:

```text
ssh root@192.168.1.18 ...
```

Result:

```text
Permission denied (publickey,password).
```

Therefore the installed URCap files and any OnRobot installation directory
cannot currently be verified from Ubuntu over SSH.

## Next Evidence Needed

The discovery/device-binding question is now answered by the photos. The next
evidence needed is variable exposure, still with the robot stopped:

1. Look for an OnRobot/FT variables, register, script function, or RTDE output
   mapping page in PolyScope.
2. Record exact variable/register names if present.
3. Do not run a template or force node just to expose variables.
4. If variables require a program to run, write a separate no-motion test plan
   before execution.

Stop if PolyScope asks to run a template/example program, update firmware,
perform `F/T Zero`, perform `F/T Set TCP`, enable hand guide, or overwrite robot
TCP/payload.
