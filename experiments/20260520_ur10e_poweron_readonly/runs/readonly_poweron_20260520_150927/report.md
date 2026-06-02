# UR10e + OnRobot Read-Only Power-On Check

- Run directory: `/home/andy/ur10e_ros2_ws/experiments/20260520_ur10e_poweron_readonly/runs/readonly_poweron_20260520_150927`
- Generated: `2026-05-20T15:09:52`
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
  "ok": false,
  "error": "TimeoutError: timed out",
  "host": "192.168.1.18",
  "port": 29999,
  "command": [
    "/usr/bin/python3",
    "scripts/check_dashboard_state.py",
    "--host",
    "192.168.1.18",
    "--json-only"
  ],
  "returncode": 0
}
```

## Payload/TCP Readback

```json
{
  "ok": false,
  "error": "TimeoutError: timed out",
  "host": "192.168.1.18",
  "port": 30004,
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
  "error": "URLError: <urlopen error timed out>"
}
```

## Capture

```json
{
  "attempted": false,
  "reason": "missing explicit operator confirmations"
}
```
