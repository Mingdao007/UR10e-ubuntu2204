# UR10e + OnRobot Read-Only Power-On Check

- Run directory: `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/runs/readonly_poweron_20260520_151436`
- Generated: `2026-05-20T15:14:42`
- UR10e power state: inferred from network/Dashboard reachability in this run
- Robot motion: none commanded by this script
- UR writes: none; payload/TCP were only read
- UR zero_ftsensor(): not called
- OnRobot writes: none; logger reads `/version` and Socket.IO polling only
- Same static posture capture: `False`

## Operator Physical Notes

Fill these in immediately after the run:

- Stack-up: UR10e flange -> OnRobot HEX-E -> adapter -> custom EOAT -> KSM-8N
- Tool/environment contact during capture:
- Cable slack at J4/J5/J6:
- Connector strain or cable rubbing:
- Any installation disturbance:

## Dashboard

```json
{
  "banner": "Connected: Universal Robots Dashboard Server",
  "is in remote control": "false",
  "safetymode": "Safetymode: NORMAL",
  "robotmode": "Robotmode: RUNNING",
  "running": "Program running: false",
  "programState": "STOPPED <unnamed>",
  "get loaded program": "Loaded program: /programs/<unnamed>.urp",
  "PolyscopeVersion": "URSoftware 5.11.9.1010452 (Jan 24 2022)"
}
```

## Payload/TCP Readback

```json
{
  "ok": true,
  "host": "192.168.1.18",
  "port": 30004,
  "payload": 0.009,
  "payload_cog": [
    0.0,
    0.0,
    0.0
  ],
  "tcp_offset": [
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0
  ],
  "actual_TCP_force": {
    "Fx": 0.2192660364600649,
    "Fy": 0.5601159954134968,
    "Fz": -0.10959099324710186,
    "Tx": -0.019525670334682874,
    "Ty": -0.00022000413736301808,
    "Tz": 0.0022603352460179066
  },
  "force_norm_N": 0.6114063369510044,
  "torque_norm_Nm": 0.019657296845282734,
  "command": [
    "/usr/bin/python3",
    "scripts/read_payload_tcp_state.py",
    "--host",
    "192.168.1.18",
    "--json-only"
  ],
  "returncode": 0
}
```

## OnRobot Version Check

```json
{
  "ok": false,
  "url": "http://192.168.1.1/version",
  "error": "URLError: <urlopen error [Errno 113] No route to host>"
}
```

## Capture

```json
{
  "attempted": false,
  "reason": "missing explicit operator confirmations"
}
```
