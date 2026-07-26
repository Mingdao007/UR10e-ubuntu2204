#!/usr/bin/env python3
"""Read-only OnRobot HEX-E Socket.IO polling logger.

This script only reads the Compute Box web stream. It must not call OnRobot
bias, zero, auto-calibration, firmware, or configuration endpoints.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev


FIELDS = [
    "t_s",
    "wall_time_utc",
    "sample_counter",
    "name",
    "serial_number",
    "status",
    "authenticated",
    "bias",
    "fxN",
    "fyN",
    "fzN",
    "txNm",
    "tyNm",
    "tzNm",
]

AXES = ["fxN", "fyN", "fzN", "txNm", "tyNm", "tzNm"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def get_text(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def socket_base(host: str) -> str:
    host = host.strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return f"{host}/socket.io/"
    return f"http://{host}/socket.io/"


def version_url(host: str) -> str:
    host = host.strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return f"{host}/version"
    return f"http://{host}/version"


def handshake(base_url: str, timeout: float) -> str:
    url = base_url + "?" + urllib.parse.urlencode(
        {"EIO": "3", "transport": "polling", "t": str(int(time.time() * 1000))}
    )
    text = get_text(url, timeout)
    match = re.search(r'\{"sid":"([^"]+)"', text)
    if not match:
        raise RuntimeError(f"Socket.IO handshake returned no sid: {text[:300]}")
    return match.group(1)


def extract_message_packet(text: str) -> dict | None:
    marker = '42["message",'
    start = text.find(marker)
    if start < 0:
        return None
    fragment = text[start + len(marker) :]
    depth = 0
    end = None
    in_string = False
    escaped = False
    for index, char in enumerate(fragment):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
    if end is None:
        return None
    return json.loads(fragment[:end])


def root_device(packet: dict) -> dict:
    return packet.get("hardware_bus", {}).get("devices", {}).get("root_device") or {}


def append_event(events_path: Path, event: dict) -> None:
    event = {"wall_time_utc": utc_now(), **event}
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def coerce_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def coerce_int(value: object) -> int | object | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def coerce_bool(value: object) -> bool | object | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return value


def read_csv_rows(csv_path: Path) -> list[dict]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def axis_stats(rows: list[dict], axis: str) -> dict | None:
    values = [coerce_float(row.get(axis)) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return {
        "mean": mean(values),
        "std": pstdev(values),
        "min": min(values),
        "max": max(values),
        "first": values[0],
        "last": values[-1],
        "last_minus_first": values[-1] - values[0],
    }


def baseline(rows: list[dict], baseline_s: float) -> dict[str, float]:
    base_rows = [row for row in rows if coerce_float(row.get("t_s")) is not None and coerce_float(row["t_s"]) <= baseline_s]
    if not base_rows:
        base_rows = rows[: max(1, min(len(rows), 100))]
    result = {}
    for axis in AXES:
        values = [coerce_float(row.get(axis)) for row in base_rows]
        values = [value for value in values if value is not None]
        if values:
            result[axis] = mean(values)
    return result


def write_drift_bins(rows: list[dict], output_path: Path, bin_s: float, baseline_values: dict[str, float]) -> list[dict]:
    bins: dict[int, list[dict]] = {}
    for row in rows:
        t_s = coerce_float(row.get("t_s"))
        if t_s is None:
            continue
        bins.setdefault(int(t_s // bin_s), []).append(row)

    records = []
    for bin_index in sorted(bins):
        bin_rows = bins[bin_index]
        record = {
            "bin_start_s": bin_index * bin_s,
            "bin_end_s": (bin_index + 1) * bin_s,
            "row_count": len(bin_rows),
        }
        for axis in AXES:
            values = [coerce_float(row.get(axis)) for row in bin_rows]
            values = [value for value in values if value is not None]
            if values:
                record[f"{axis}_mean"] = mean(values)
                if axis in baseline_values:
                    record[f"{axis}_mean_minus_initial_baseline"] = mean(values) - baseline_values[axis]
        records.append(record)

    fieldnames = ["bin_start_s", "bin_end_s", "row_count"]
    for axis in AXES:
        fieldnames.extend([f"{axis}_mean", f"{axis}_mean_minus_initial_baseline"])
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    return records


def plot_rows(rows: list[dict], output_path: Path, baseline_values: dict[str, float]) -> dict[str, str | None]:
    plot_paths = {
        "full": output_path,
        "fz": output_path.with_name("fz.png"),
        "fxy": output_path.with_name("fxy.png"),
    }
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - depends on host packages
        return {name: repr(exc) for name in plot_paths}

    t = [coerce_float(row.get("t_s")) for row in rows]
    t = [value for value in t if value is not None]
    if not t:
        return {name: "no timestamp rows to plot" for name in plot_paths}

    errors: dict[str, str | None] = {name: None for name in plot_paths}

    try:
        fig, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
        for axis in ["fxN", "fyN", "fzN"]:
            y = [coerce_float(row.get(axis)) for row in rows]
            axes[0].plot(t[: len(y)], y, label=axis)
            if axis in baseline_values:
                axes[0].axhline(baseline_values[axis], alpha=0.15)
        axes[0].set_ylabel("Force (N)")
        axes[0].grid(True, alpha=0.3)
        axes[0].legend(loc="best")

        for axis in ["txNm", "tyNm", "tzNm"]:
            y = [coerce_float(row.get(axis)) for row in rows]
            axes[1].plot(t[: len(y)], y, label=axis)
            if axis in baseline_values:
                axes[1].axhline(baseline_values[axis], alpha=0.15)
        axes[1].set_ylabel("Torque (Nm)")
        axes[1].set_xlabel("time (s)")
        axes[1].grid(True, alpha=0.3)
        axes[1].legend(loc="best")

        fig.suptitle("OnRobot HEX-E read-only bench drift")
        fig.tight_layout()
        fig.savefig(plot_paths["full"], dpi=160)
        plt.close(fig)
    except Exception as exc:  # pragma: no cover - plotting depends on local data/packages
        errors["full"] = repr(exc)

    try:
        fig, ax = plt.subplots(figsize=(13, 4.8))
        y = [coerce_float(row.get("fzN")) for row in rows]
        ax.plot(t[: len(y)], y, label="Fz", linewidth=1.2)
        if "fzN" in baseline_values:
            ax.axhline(baseline_values["fzN"], alpha=0.18, label="initial baseline")
        ax.set_title("OnRobot HEX-E Fz")
        ax.set_xlabel("time (s)")
        ax.set_ylabel("Fz (N)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")
        fig.tight_layout()
        fig.savefig(plot_paths["fz"], dpi=160)
        plt.close(fig)
    except Exception as exc:  # pragma: no cover
        errors["fz"] = repr(exc)

    try:
        fig, ax = plt.subplots(figsize=(13, 4.8))
        for axis in ["fxN", "fyN"]:
            y = [coerce_float(row.get(axis)) for row in rows]
            ax.plot(t[: len(y)], y, label=axis, linewidth=1.2)
            if axis in baseline_values:
                ax.axhline(baseline_values[axis], alpha=0.15)
        ax.set_title("OnRobot HEX-E Fx/Fy")
        ax.set_xlabel("time (s)")
        ax.set_ylabel("Force (N)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")
        fig.tight_layout()
        fig.savefig(plot_paths["fxy"], dpi=160)
        plt.close(fig)
    except Exception as exc:  # pragma: no cover
        errors["fxy"] = repr(exc)

    return errors


def summarize_run(run_dir: Path, baseline_s: float, bin_s: float) -> dict:
    csv_path = run_dir / "raw_wrench.csv"
    rows = read_csv_rows(csv_path)
    baseline_values = baseline(rows, baseline_s)
    drift_bins = write_drift_bins(rows, run_dir / "drift_10min_bins.csv", bin_s, baseline_values)
    plot_paths = {
        "full": run_dir / "force_torque.png",
        "fz": run_dir / "fz.png",
        "fxy": run_dir / "fxy.png",
    }
    plot_errors = plot_rows(rows, plot_paths["full"], baseline_values)

    events_path = run_dir / "events.jsonl"
    events = []
    if events_path.exists():
        with events_path.open(encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]

    summary = {
        "rows": len(rows),
        "duration_s": (coerce_float(rows[-1]["t_s"]) - coerce_float(rows[0]["t_s"])) if len(rows) > 1 else 0,
        "first_wall_time_utc": rows[0]["wall_time_utc"] if rows else None,
        "last_wall_time_utc": rows[-1]["wall_time_utc"] if rows else None,
        "device": rows[-1]["name"] if rows else None,
        "serial_number": rows[-1]["serial_number"] if rows else None,
        "status": coerce_int(rows[-1]["status"]) if rows else None,
        "authenticated": coerce_bool(rows[-1]["authenticated"]) if rows else None,
        "bias": coerce_bool(rows[-1]["bias"]) if rows else None,
        "event_count": len(events),
        "reconnect_count": sum(1 for event in events if event.get("event") == "socket_handshake_ok"),
        "error_count": sum(1 for event in events if "error" in event.get("event", "")),
        "baseline_s": baseline_s,
        "baseline_values": baseline_values,
        "axis_stats": {axis: axis_stats(rows, axis) for axis in AXES},
        "drift_bin_count": len(drift_bins),
        "plots": {name: str(path) for name, path in plot_paths.items()},
        "plot_errors": plot_errors,
        "plot_error": plot_errors.get("full"),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_logger(args: argparse.Namespace) -> int:
    run_dir = Path(args.out_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    csv_path = run_dir / "raw_wrench.csv"
    events_path = run_dir / "events.jsonl"
    metadata_path = run_dir / "metadata.json"
    notes_path = run_dir / "run_notes.md"

    metadata = {
        "host": args.host,
        "duration_s": args.duration_s,
        "poll_interval_s": args.poll_interval_s,
        "flush_every_s": args.flush_every_s,
        "started_wall_time_utc": utc_now(),
        "hostname": socket.gethostname(),
        "read_only_policy": "No bias/zero/autocalib/firmware/config endpoints are called by this script.",
    }

    try:
        metadata["compute_box_version_response"] = get_text(version_url(args.host), args.timeout_s)
        append_event(events_path, {"event": "version_ok", "response": metadata["compute_box_version_response"]})
    except Exception as exc:
        metadata["compute_box_version_error"] = repr(exc)
        append_event(events_path, {"event": "version_error", "error": repr(exc)})

    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    notes_path.write_text(
        "# OnRobot HEX-E Bench Drift Run Notes\n\n"
        "- Read-only run. No zero/bias/autocalib performed by logger.\n"
        "- Record manual disturbances, cable touches, room-temperature changes, and power events below.\n\n",
        encoding="utf-8",
    )

    base_url = socket_base(args.host)
    end_time = time.time() + args.duration_s
    t0 = time.time()
    next_flush = time.time() + args.flush_every_s
    next_status = time.time() + args.status_every_s
    sid = None
    last_counter = None
    rows_written = 0

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        handle.flush()

        while time.time() < end_time:
            if sid is None:
                try:
                    sid = handshake(base_url, args.timeout_s)
                    append_event(events_path, {"event": "socket_handshake_ok", "t_s": time.time() - t0, "sid": sid})
                except Exception as exc:
                    append_event(events_path, {"event": "socket_handshake_error", "t_s": time.time() - t0, "error": repr(exc)})
                    time.sleep(args.reconnect_delay_s)
                    continue

            poll_url = base_url + "?" + urllib.parse.urlencode(
                {"EIO": "3", "transport": "polling", "sid": sid, "t": str(int(time.time() * 1000))}
            )
            try:
                text = get_text(poll_url, args.timeout_s)
                packet = extract_message_packet(text)
                if packet is None:
                    time.sleep(args.poll_interval_s)
                    continue
                device = root_device(packet)
                if "fxN" not in device:
                    time.sleep(args.poll_interval_s)
                    continue
                counter = device.get("sample_counter")
                if counter == last_counter:
                    time.sleep(args.poll_interval_s)
                    continue
                last_counter = counter
                row = {
                    "t_s": time.time() - t0,
                    "wall_time_utc": utc_now(),
                    "sample_counter": counter,
                    "name": device.get("name"),
                    "serial_number": device.get("serial_number"),
                    "status": device.get("status"),
                    "authenticated": device.get("authenticated"),
                    "bias": device.get("bias"),
                    "fxN": device.get("fxN"),
                    "fyN": device.get("fyN"),
                    "fzN": device.get("fzN"),
                    "txNm": device.get("txNm"),
                    "tyNm": device.get("tyNm"),
                    "tzNm": device.get("tzNm"),
                }
                writer.writerow(row)
                rows_written += 1

                if time.time() >= next_flush:
                    handle.flush()
                    next_flush = time.time() + args.flush_every_s

                if time.time() >= next_status:
                    print(
                        f"[{utc_now()}] rows={rows_written} t_s={row['t_s']:.1f} "
                        f"serial={row['serial_number']} status={row['status']} bias={row['bias']} "
                        f"F=({row['fxN']},{row['fyN']},{row['fzN']}) "
                        f"T=({row['txNm']},{row['tyNm']},{row['tzNm']})",
                        flush=True,
                    )
                    append_event(events_path, {"event": "status", "t_s": row["t_s"], "rows": rows_written})
                    next_status = time.time() + args.status_every_s

            except urllib.error.HTTPError as exc:
                append_event(events_path, {"event": "poll_http_error", "t_s": time.time() - t0, "code": exc.code, "error": repr(exc)})
                sid = None
                time.sleep(args.reconnect_delay_s)
            except Exception as exc:
                append_event(events_path, {"event": "poll_error", "t_s": time.time() - t0, "error": repr(exc)})
                sid = None
                time.sleep(args.reconnect_delay_s)

        handle.flush()

    append_event(events_path, {"event": "logger_finished", "t_s": time.time() - t0, "rows": rows_written})
    summary = summarize_run(run_dir, args.baseline_s, args.bin_s)
    print(json.dumps(summary, indent=2), flush=True)
    print(f"run_dir {run_dir}", flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.1.1")
    parser.add_argument("--duration-s", type=float, required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--timeout-s", type=float, default=8.0)
    parser.add_argument("--poll-interval-s", type=float, default=0.05)
    parser.add_argument("--reconnect-delay-s", type=float, default=0.3)
    parser.add_argument("--flush-every-s", type=float, default=60.0)
    parser.add_argument("--status-every-s", type=float, default=1800.0)
    parser.add_argument("--baseline-s", type=float, default=600.0)
    parser.add_argument("--bin-s", type=float, default=600.0)
    return parser.parse_args()


def main() -> int:
    return run_logger(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
