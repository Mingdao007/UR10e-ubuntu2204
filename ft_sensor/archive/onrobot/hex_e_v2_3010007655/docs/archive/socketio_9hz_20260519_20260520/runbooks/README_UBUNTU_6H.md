# OnRobot HEX-E Ubuntu 6 h Bench Drift Package

This package is for a standalone, no-motion, no-install OnRobot HEX-E bench drift run on Ubuntu `andy7`.

## Hardware State

- Connect `HEX-E -> sensor cable -> Compute Box`.
- Connect `Compute Box Ethernet -> Ubuntu enp3s0`.
- Power the Compute Box with the 24 V adapter only after the sensor cable and Ethernet are seated.
- Do not connect the bare-wire 24 V robot power cable.
- Do not install the sensor on UR10e for this run.

## Safety Policy

The logger is read-only. It reads `http://192.168.1.1/socket.io/` and `/version`.

Do not call:

- `bias` / `zero` endpoints
- `autocalib`
- firmware update endpoints
- DIP switch or configuration changes

## Expected Ubuntu Network

Current preflight found:

- Ethernet interface: `enp3s0`
- Ubuntu address: `192.168.1.10/24`
- Compute Box fixed IP: `192.168.1.1`

Check before running:

```bash
ip -br addr show enp3s0
ping -c 4 192.168.1.1
curl -s http://192.168.1.1/version
```

## Dry Run

From this directory:

```bash
RUN_ROOT="$PWD/measurements/bench_drift_6h_$(date +%Y%m%d_%H%M)"
mkdir -p "$RUN_ROOT/dry_run_2min"
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s 120 \
  --out-dir "$RUN_ROOT/dry_run_2min" \
  --status-every-s 30
```

Proceed only if `summary.json` reports `status=0`, `authenticated=true`, `bias=False/false`, and CSV rows are increasing.

## 6 h Run

```bash
RUN_ROOT="$PWD/measurements/bench_drift_6h_$(date +%Y%m%d_%H%M)"
mkdir -p "$RUN_ROOT/main_6h"
python3 tools/onrobot_socketio_logger.py \
  --host 192.168.1.1 \
  --duration-s 21600 \
  --out-dir "$RUN_ROOT/main_6h" \
  --status-every-s 1800
```

Outputs:

- `raw_wrench.csv`
- `events.jsonl`
- `metadata.json`
- `summary.json`
- `drift_10min_bins.csv`
- `force_torque.png`
- `run_notes.md`

## Identity Note

The live socket stream previously reported serial `HEXEB806`. The archived label/certificate evidence includes `3010007655` and `HEXEB306` style identifiers. Treat this as a required identity cross-check before using the data as final calibration evidence.
